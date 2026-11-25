"""UI-independent course state and operations for the native workbench."""
from __future__ import annotations

import copy
import logging
import queue
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .course_catalog import CourseCatalogMixin
from .diagnostics import redact_log
from .model_api import probe_model
from .task_grading import MIN_TASK_SCORE, needs_live_conversation, score_text, selectable_task


def log_level(message):
    """Classify the backend's plain-text messages for the native log view."""
    text = message.casefold()
    if any(word in text for word in ("失败", "异常", "错误", "阻断")) or re.search(r"\b(error|exception|traceback|fatal)\b", text):
        return "error"
    if (any(word in text for word in ("警告", "重试", "超时", "跳过", "未找到", "未能", "没有找到", "请手动"))
            or re.search(r"(未|没有).{0,6}(完成|成功|提交|就绪)", text)
            or re.search(r"\b(warning|warn|retry|timeout|skipped)\b", text)):
        return "warning"
    if any(word in text for word in ("成功", "完成", "就绪", "已提交")):
        return "success"
    return "info"


class WorkbenchController(CourseCatalogMixin):
    """Python state and operations shared with the existing automation backend."""
    def __init__(self, driver=None, solver=None, bot=None, app_globals=None):
        self.driver, self.solver, self.bot = driver, solver, bot
        self._globals = app_globals or {}
        self.logger = self._globals.get("logger")
        redact_log.add_config(getattr(bot, "config", None))
        self.gui_log_queue = self._globals.get("gui_log_queue", queue.Queue())
        for name in ("WebDriverWait", "EC", "By", "NoSuchElementException", "WebDriverHelper"):
            setattr(self, name, self._globals.get(name))
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._stop_requested = threading.Event()
        self._tasks = []
        self._selected = set()
        self._logs = []
        self._log_id = 0
        self._ready = False
        self._operation = None
        self._worker = None
        self._status = "正在登录"
        self._error = ""
        self._exclude_ungraded = False
        self._completed = 0
        self._total = 0
        self._started = 0
        self._active_task = ""
        self._api_status, self._api_ok, self._api_fail = "正在检测接口", False, False
        self._api_config_error = False
        self._log_worker = threading.Thread(target=self._collect_logs, daemon=True, name="workbench-logs")
        self._log_worker.start()
        threading.Thread(target=self._probe_api, daemon=True, name="workbench-api").start()

    def _probe_api(self):
        config = getattr(self.bot, "config", None)
        base, key, model = (getattr(config, name, "") for name in ("base_url", "api_key", "model"))
        result = probe_model(base, key, model)
        with self._lock:
            if self._closed.is_set():
                return
            self._api_status = result.message if config is not None else "未配置 API"
            self._api_ok = result.ok
            self._api_fail = config is not None and not result.ok
            self._api_config_error = config is not None and result.config_error
            level = "success" if self._api_ok else "warning" if self._api_fail else "info"
            self._add_log(f"模型接口检测：{self._api_status}", level)

    @staticmethod
    def after(ms, callback):
        if ms:
            timer = threading.Timer(ms / 1000, callback)
            timer.daemon = True
            timer.start()
        else:
            callback()

    def enable_scan_button(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._ready = True
            self._status = "就绪"
        self._add_log("账号登录成功，等待扫描课程。", "success")

    def _redact(self, message):
        return redact_log(message)

    def current_run_log(self):
        for handler in getattr(self.logger, "handlers", ()):
            if isinstance(handler, logging.FileHandler):
                try:
                    handler.flush()
                    return Path(handler.baseFilename).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
        return None

    def _add_log(self, message, level="info"):
        message = self._redact(message)
        with self._lock:
            self._log_id += 1
            self._logs.append({"id": self._log_id, "time": datetime.now().strftime("%H:%M:%S"),
                               "message": str(message), "level": level})
            self._logs = self._logs[-500:]

    def _collect_logs(self):
        while not self._closed.is_set():
            try:
                message = str(self.gui_log_queue.get(timeout=.5)).strip()
            except queue.Empty:
                continue
            if not message or set(message) <= {"=", "*", "-", " "}:
                continue
            self._add_log(message, log_level(message))

    def snapshot(self):
        with self._lock:
            tasks = [{"id": t["id"], "title": t.get("l1_title") or t.get("display", ""),
                      "unit": t.get("unit_label") or "当前单元", "unitName": t.get("unit_name", ""),
                      "section": t.get("section") or t.get("l2_title") or "课程任务",
                      "required": bool(t.get("is_compulsory")),
                      "scored": t.get("is_scored"), "score": t.get("score"),
                      "score_abnormal": any(score < MIN_TASK_SCORE for score in self._task_scores(t)),
                      "score_pending": self._has_pending_score(t),
                      "manual_conversation": needs_live_conversation(t),
                      "selectable": selectable_task(t, self._exclude_ungraded),
                      "progress": t.get("progress"), "status": t.get("status", "unknown"),
                      "selected": t["id"] in self._selected} for t in self._tasks]
            config = getattr(self.bot, "config", SimpleNamespace(username="", model=""))
            username = str(getattr(config, "username", ""))
            account = f"{username[:3]}••••{username[-4:]}" if len(username) > 7 else "已连接账号" if username else "未连接账号"
            from .meta import APP_NAME, APP_VERSION, GITHUB_DISPLAY, GITHUB_URL
            return copy.deepcopy({
                "appName": self._globals.get("APP_NAME", APP_NAME),
                "version": self._globals.get("APP_VERSION", APP_VERSION),
                "githubDisplay": self._globals.get("GITHUB_DISPLAY", GITHUB_DISPLAY),
                "githubUrl": self._globals.get("GITHUB_URL", GITHUB_URL),
                "ready": self._ready, "busy": self._operation is not None, "operation": self._operation,
                "status": self._status, "error": self._error,
                "excludeUngraded": self._exclude_ungraded, "debug": bool(self._globals.get("DEBUG_MODE")),
                "tasks": tasks, "logs": self._logs, "account": account,
                "model": getattr(config, "model", "") or "未配置",
                "apiStatus": self._api_status, "apiOk": self._api_ok, "apiFail": self._api_fail,
                "progress": {"completed": self._completed, "total": self._total,
                             "started": self._started, "activeTask": self._active_task},
            })

    def set_selection(self, ids, selected):
        with self._lock:
            if self._operation:
                return {"ok": False, "error": "请先停止当前操作，再调整任务。"}
            valid = {t["id"] for t in self._tasks
                     if not selected or selectable_task(t, self._exclude_ungraded)}
            if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                return {"ok": False, "error": "任务选择无效。"}
            chosen = set(ids) & valid
            self._selected.update(chosen) if selected else self._selected.difference_update(chosen)
            return {"ok": True}

    def select_tasks(self, mode, ids):
        if mode not in ("all", "required", "pending", "none") or not isinstance(ids, list):
            return {"ok": False, "error": "选择方式无效。"}
        with self._lock:
            if self._operation:
                return {"ok": False, "error": "当前操作进行中。"}
            scope = {item for item in ids if isinstance(item, str)}
            self._selected.difference_update(scope)
            if mode != "none":
                self._selected.update(t["id"] for t in self._tasks if t["id"] in scope
                                      and selectable_task(t, self._exclude_ungraded)
                                      and (mode == "all" or t.get("is_compulsory"))
                                      and (mode != "pending" or t.get("status") != "done"))
            return {"ok": True}

    def set_exclude_ungraded(self, enabled):
        with self._lock:
            if self._operation:
                return {"ok": False, "error": "当前操作进行中。"}
            self._exclude_ungraded = bool(enabled)
            if enabled:
                self._selected.difference_update(t["id"] for t in self._tasks if t.get("is_scored") is False)
        return {"ok": True}

    def start_action(self, action, *, task_ids=None):
        if action not in ("scan", "scan_unit", "run", "quick"):
            return {"ok": False, "error": "无法识别此操作。"}
        with self._lock:
            if self._closed.is_set() or not self._ready or self._operation:
                return {"ok": False, "error": "请等待登录或当前操作完成。"}
            if action in ("run", "quick") and self._api_config_error:
                return {"ok": False, "error": self._api_status + "。保存模型配置后重新打开工作台。"}
            selected = []
            if action == "run":
                selected_ids = self._selected if task_ids is None else self._selected.intersection(task_ids)
                selected = [task for task in self._tasks if task["id"] in selected_ids
                            and selectable_task(task, self._exclude_ungraded)]
                if not selected:
                    return {"ok": False, "error": "请先在当前列表选择至少一项任务。"}
            self._operation = action
            self._error = ""
            self._completed = 0
            self._started = 0
            self._active_task = ""
            self._total = len(selected)
            self._stop_requested.clear()
            if self.solver:
                self.solver.clear_stop()
            self._status = "正在扫描" if action.startswith("scan") else "正在处理"
            self._worker = threading.Thread(target=self._run_operation, args=(action, selected),
                                            daemon=True, name="course-operation")
            self._worker.start()
        return {"ok": True}

    def _watcher(self):
        return getattr(self.bot, "popup_watcher", None)

    def _run_operation(self, action, selected):
        watcher = self._watcher()
        if watcher is not None:
            watcher.pause()
        try:
            if action.startswith("scan"):
                tasks = self.scan_tasks(current_unit_only=action == "scan_unit")
                if not tasks:
                    raise RuntimeError("没有找到课程任务。请先在浏览器中打开课程目录，再重新扫描。")
                with self._lock:
                    for index, task in enumerate(tasks):
                        task["id"] = f"task-{index}"
                    self._tasks = tasks
                    self._selected = {t["id"] for t in tasks if t.get("is_compulsory")
                                      and t.get("status") != "done" and selectable_task(t, self._exclude_ungraded)}
                self._add_log(f"扫描完成，共发现 {len(tasks)} 项任务。", "success")
            elif action == "run":
                self._run_selected_tasks(selected)
            else:
                self.solver.processed_hashes.clear()
                if not self.solver.solve_current_page() and not self.solver._should_stop():
                    raise RuntimeError("当前页面未处理完成，请查看运行日志。")
            with self._lock:
                stopped = self._is_stopped()
                self._status = "已停止" if stopped else "就绪"
        except Exception as exc:
            if self._closed.is_set():
                return
            self._add_log(str(exc), "error")
            with self._lock:
                self._status = "需要检查"
                self._error = self._redact(exc)[:250]
            if self.logger:
                self.logger.error("Workbench operation failed", exc_info=True)
        finally:
            if watcher is not None:
                watcher.resume()
            with self._lock:
                self._active_task = ""
                self._operation = None

    def _run_selected_tasks(self, selected):
        started = {}

        def on_started(task):
            started[task["id"]] = task
            with self._lock:
                self._started = len(started)
                self._active_task = task.get("l1_title") or task.get("display", "")

        for task in selected:
            task.pop("_publish_redo_attempted", None)
        self.solver.process_selected_tabs(selected, on_task_done=self._task_done, on_task_started=on_started)
        attempted = list(started.values())
        unconfirmed = None
        if not self._is_stopped() and not self._closed.is_set():
            unconfirmed = self._verify_task_records(attempted)
            retryable = [task for task in (unconfirmed or []) if self._can_retry_after_verification(task)]
            if retryable and not getattr(self.solver, "_aborted_by_errors", False) and not self._is_stopped():
                self._add_log(f"平台记录确认仍有 {len(retryable)} 项作答或音视频待处理，补做一轮。", "warning")
                with self._lock:
                    self._status = "正在补做未确认任务"
                clear_media = getattr(self.solver, "clear_watched_media_for_task", None)
                if clear_media is not None:
                    for task in retryable:
                        media = [g for g in task.get("_record_groups") or [] if g["kind"] in ("video", "audio")]
                        if (task.get("_record_matched") and media and all(g["progress"] != 100 for g in media)):
                            clear_media(task.get("l1_title") or "", scope_key=task.get("_media_scope_key") or "")
                self.solver.process_selected_tabs(retryable, on_task_done=self._task_done, on_task_started=on_started)
                if not self._is_stopped():
                    unconfirmed = self._verify_task_records(attempted)
        if self._closed.is_set() or not attempted:
            return
        if self._is_stopped():
            unconfirmed = self._sync_tasks_from_server(attempted, after_stop=True)
        if unconfirmed is not None:
            self._log_task_records(attempted, unconfirmed)
        self._add_log(f"本轮开始 {len(attempted)}/{len(selected)} 项，平台确认完成 {self._completed} 项。",
                      "info" if unconfirmed is not None else "warning")

    def _is_stopped(self):
        return self._stop_requested.is_set() or (self.solver is not None and self.solver._should_stop())

    def _verify_task_records(self, tasks):
        remaining = self._sync_tasks_from_server(tasks)
        if remaining and not self._is_stopped() and not self._stop_requested.wait(2):
            # 首次读取可能早于平台写入，先再读一次记录，再决定是否有补做条件。
            latest = self._sync_tasks_from_server(tasks)
            if latest is not None:
                remaining = latest
        return remaining

    def _can_retry_after_verification(self, task):
        if (not task.get("_catalog_matched") or task.get("_submission_unconfirmed")
                or task.get("_publish_redo_attempted")
                or not selectable_task(task, self._exclude_ungraded)):
            return False
        groups = task.get("_record_groups") or []
        if groups:
            return any(g["required"] and g["progress"] != 100
                       and g["kind"] in ("task", "ut", "video", "audio") for g in groups)
        return task.get("progress") != 100

    @staticmethod
    def _task_scores(task):
        if task.get("is_scored") is False:
            return []
        groups = task.get("_record_groups") or []
        if groups:
            return [g["score"] for g in groups if g["scored"] is True and g["score"] is not None]
        return [task["score"]] if task.get("score") is not None else []

    @staticmethod
    def _has_pending_score(task):
        if task.get("is_scored") is not True:
            return False
        groups = task.get("_record_groups") or []
        if groups:
            return any(g["scored"] is True and g["score"] is None for g in groups)
        return task.get("score") is None

    def _log_task_records(self, tasks, unconfirmed):
        labels = {"task": "作答", "ut": "单元测试", "text": "阅读",
                  "discussion": "讨论", "video": "视频", "audio": "音频"}
        for task in unconfirmed:
            name = " · ".join(filter(None, (task.get("section"), task.get("l1_title"))))
            if not task.get("_catalog_matched"):
                detail = "未匹配到课程目录任务"
            elif not task.get("_record_matched"):
                detail = "学习记录未匹配，仍待核验"
            else:
                pending = [f'{labels.get(g["kind"], "学习项")}「{g["name"] or "未命名"}」'
                           for g in task.get("_record_groups", [])
                           if g["required"] and g["progress"] != 100]
                detail = "缺平台记录的项目：" + "、".join(pending) if pending else "目录尚未确认完成"
            message = f"{name}｜{detail}"
            self._add_log(message, "warning")
            if self.logger:
                self.logger.warning("%s", message)
        if not unconfirmed:
            self._add_log("课程目录已确认本轮任务完成，计分情况以学习记录为准。", "info")
        for task in tasks:
            if not task.get("_catalog_matched") or not task.get("_record_matched"):
                continue
            for score in self._task_scores(task):
                if score < MIN_TASK_SCORE:
                    message = (f'{task.get("display") or task.get("l1_title")}｜'
                               f'平台登记 {score_text(score)} 分，低于 {MIN_TASK_SCORE} 分目标')
                    self._add_log(message, "warning")
                    if self.logger:
                        self.logger.warning("%s", message)

    def _sync_tasks_from_server(self, processed_tasks, *, after_stop=False):
        """任务跑完后返回课程目录，用 U 校园真实状态回写界面，返回平台仍未确认完成的任务列表。"""
        if not processed_tasks or self.driver is None or self._closed.is_set():
            return None
        directory_url = next((t.get("_directory_url") for t in processed_tasks if t.get("_directory_url")), "")
        unit_indices = {t.get("_unit_idx") for t in processed_tasks if t.get("_unit_idx") is not None}
        if not directory_url or not unit_indices:
            return None
        with self._lock:
            self._active_task = ""
            self._status = "已停止处理，正在核验平台状态" if after_stop else "正在核验平台状态"
        self._add_log("正在返回课程目录核验 U 校园真实完成状态…", "info")
        # 等待最后一条提交的后端学习记录落库
        wait_event = self._closed if after_stop else self._stop_requested
        if wait_event.wait(1.2) or self._closed.is_set():
            return None
        server_tabs = self.refresh_units_from_server(directory_url, unit_indices)
        if self._closed.is_set():
            return None
        if not server_tabs:
            self._add_log("未能从课程目录读取平台状态，请手动点击「扫描当前单元」核对。", "warning")
            return None

        def task_key(task):
            return (task.get("_unit_id") or task.get("_unit_idx"),
                    (task.get("section") or "").strip(), task.get("l1_title"))

        by_path = defaultdict(list)
        for row in server_tabs:
            by_path[task_key(row)].append(row)
        processed_ids = {t.get("id") for t in processed_tasks if t.get("id")}
        unconfirmed = []
        with self._lock:
            for task in self._tasks:
                if task.get("_unit_idx") not in unit_indices:
                    continue
                matches = by_path.get(task_key(task), [])
                task["_catalog_matched"] = len(matches) == 1
                if len(matches) != 1:
                    if task["id"] in processed_ids:
                        unconfirmed.append(task)
                    continue
                hit = matches[0]
                real_status = hit.get("status", "unknown")
                task["status"] = real_status
                task["status_text"] = hit.get("status_text") or task.get("status_text", "")
                if hit.get("display"):
                    task["display"] = hit["display"]
                task["_record_matched"] = bool(hit.get("_record_matched"))
                if task["_record_matched"]:
                    for field in ("is_scored", "score", "progress", "_record_groups", "_record_route"):
                        task[field] = hit.get(field)
                if real_status == "done" or not selectable_task(task, self._exclude_ungraded):
                    self._selected.discard(task["id"])
                if real_status != "done" and task["id"] in processed_ids:
                    unconfirmed.append(task)
                    if selectable_task(task, self._exclude_ungraded):
                        self._selected.add(task["id"])
            self._completed = sum(t.get("status") == "done" and t.get("_catalog_matched") is True
                                  for t in processed_tasks)
        return unconfirmed

    def _task_done(self, task):
        name = task.get("l1_title", "")
        score = task.get("session_score")
        if task.get("status") != "done" or task.get("_directory_url"):
            suffix = f"，页面回执 {score_text(score)} 分" if score is not None else ""
            self._add_log(f"本轮已处理：{name}，待课程目录核验{suffix}", "info")
            return
        if score is not None and score < MIN_TASK_SCORE:
            self._add_log(f"页面回执：{name} 得分 {score:g}，低于 {MIN_TASK_SCORE} 分目标", "warning")
        else:
            suffix = f"，得分 {score:g}" if score is not None else ""
            self._add_log(f"已收到页面回执：{name}{suffix}", "info")

    def stop(self):
        self._stop_requested.set()
        if self.solver:
            self.solver.request_stop()
        with self._lock:
            if self._operation:
                self._status = "正在停止"
        return {"ok": True}

    def restart_browser(self):
        with self._lock:
            if self._closed.is_set() or self._operation:
                return {"ok": False, "error": "请等待当前操作完成。"}
            self._ready = False
            self._error = ""
            self._status = "正在重启浏览器"
            self._operation = "restart"
            self._worker = threading.Thread(target=self._restart_browser, daemon=True,
                                            name="browser-restart")
            self._worker.start()
        self._add_log("正在重启课程浏览器…")
        return {"ok": True}

    def _restart_browser(self):
        succeeded = False
        try:
            if not self.bot or not hasattr(self.bot, "restart_browser"):
                raise RuntimeError("当前环境不支持重启浏览器。")
            driver = self.bot.restart_browser()
            config = getattr(self.bot, "config", None) or getattr(self.solver, "config", None)
            solver = type(self.solver)(driver, config) if self.solver else None
            with self._lock:
                self.driver = driver
                self.solver = solver
                self._tasks = []
                self._selected = set()
            self._add_log("课程浏览器已重新启动，正在重新登录…")
            if solver is not None and not self.bot._login():
                raise RuntimeError("浏览器已重启，但重新登录没有完成。")
            if self.logger:
                self.logger.info("Browser restarted")
            with self._lock:
                self._status = "就绪"
                self._error = ""
            succeeded = True
            self._add_log("课程浏览器重启完成。", "success")
        except Exception as exc:
            if self._closed.is_set():
                return
            self._add_log(str(exc), "error")
            with self._lock:
                self._status = "需要检查"
                self._error = self._redact(exc)[:250]
            if self.logger:
                self.logger.error("Restart browser failed", exc_info=True)
        finally:
            with self._lock:
                self._operation = None
                # 重建并登录成功后才恢复可操作。
                self._ready = succeeded and not self._closed.is_set()

    def close(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
        if self.bot and hasattr(self.bot, "request_shutdown"):
            self.bot.request_shutdown()
        self.stop()
        if self._log_worker is not threading.current_thread():
            self._log_worker.join(timeout=.6)
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=.8)
        if self.driver:
            driver, self.driver = self.driver, None
            def quit_driver():
                try:
                    driver.quit()
                except Exception:
                    pass
            worker = threading.Thread(target=quit_driver, daemon=True, name="browser-cleanup")
            worker.start()
            worker.join(timeout=6)
            if worker.is_alive():
                service = getattr(driver, "service", None)
                process = getattr(service, "process", None)
                if process and process.poll() is None:
                    try:
                        if sys.platform == "win32":
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           capture_output=True, timeout=3,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                        else:
                            process.terminate()
                    except (OSError, subprocess.TimeoutExpired):
                        pass
