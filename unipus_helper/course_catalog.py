"""Course directory scanning independent of UI rendering."""
import re
import time
from urllib.parse import parse_qs, urlsplit

from selenium.common.exceptions import TimeoutException

from .task_grading import (READ_GRADING_JS, assign_task_records, build_record_index,
                           grading_text, score_text)


class CourseCatalogMixin:
    """Requires a Selenium driver, solver, logger and Selenium wait helpers."""

    def scan_tasks(self, current_unit_only=False):
        tabs = self._scan_course_directory(current_unit_only)
        if tabs:
            self.gui_log_queue.put(f"课程目录页发现 {len(tabs)} 个任务")
            return tabs
        if current_unit_only:
            return []
        for l1_idx, l1_tab in enumerate(self.solver._get_level1_tabs() or []):
            if not self.WebDriverHelper.safe_click(self.driver, l1_tab["element"]):
                continue
            time.sleep(1.2)
            level2 = self.solver._get_level2_tabs()
            if not level2:
                tabs.append({"l1_idx": l1_idx, "l2_idx": -1,
                             "l1_title": l1_tab["title"], "l2_title": "",
                             "display": l1_tab["title"], "is_l2": False,
                             "is_compulsory": True, "status": "unknown"})
            else:
                for l2_idx, l2_tab in enumerate(level2):
                    tabs.append({"l1_idx": l1_idx, "l2_idx": l2_idx,
                                 "l1_title": l1_tab["title"], "l2_title": l2_tab["title"],
                                 "display": f"  - {l2_tab['title']}", "is_l2": True,
                                 "is_compulsory": True, "status": "unknown"})
        self.gui_log_queue.put(f"章节内部页发现 {len(tabs)} 个任务")
        return tabs

    TASK_ITEM_SELECTOR = '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]'

    READ_TASKS_JS = """
        const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
        if ([...document.querySelectorAll('.ant-spin-spinning')].some(el => el.getClientRects().length))
            return [];
        const all = [...document.querySelectorAll(
            '[class*="taskItemInnerLayout"], [class*="nodeItemInnerLayout"]')];
        const out = [];
        all.forEach((el, index) => {
          // 切 Unit 后旧面板仍留在 DOM 里，只取当前可见的那一份，否则任务会成倍重复
          if (!el.getClientRects().length || el.closest('.ant-tabs-tabpane-inactive, [aria-hidden="true"]'))
            return;
          const style = getComputedStyle(el);
          if (style.visibility !== 'visible' || Number(style.opacity) === 0) return;
          const nameEl = el.querySelector('[class*="taskTypeName"], [class*="nodeTypeName"]');
          const statusEl = el.querySelector('[class*="nodePassStateTip"]');
          const requireEl = el.querySelector('[class*="taskRequireIcon"], [class*="nodeRequireIcon"]');
          const path = [];
          let p = el;
          while ((p = p.parentElement)) {
            const head = p.querySelector(':scope > .ant-collapse-header');
            if (head) {
              const text = clean(head.innerText).replace(/必修|选修/g, '').trim();
              if (text) path.unshift(text);
            }
          }
          out.push({
            index,
            name: clean(nameEl && nameEl.innerText),
            status: clean(statusEl && statusEl.innerText),
            compulsory: !!requireEl,
            section: path.join(' / ')
          });
        });
        return out;
    """

    UNIT_LABEL_JS = """
        const el = arguments[0];
        const node = el.querySelector('[class*="unitName"], [class*="richName"], [class*="name-item"]');
        const title = node && (node.getAttribute('title') || node.innerText);
        return (title || el.innerText || '').replace(/\\s+/g, ' ').trim();
    """

    ACTIVE_UNIT_JS = r"""
        const el = arguments[0];
        const selected = el.getAttribute('aria-selected');
        if (selected === 'true' || selected === 'false') return selected === 'true';
        const cls = String(el.className || '');
        return /tabactive|slideractive/i.test(cls)
            || /(?:^|[\s_-])(?:active|selected|current)(?:$|[\s_-])/i.test(cls)
            || !!el.querySelector('[class*="sliderActive"]');
    """

    @staticmethod
    def _parse_unit_label(label: str):
        """把 Unit 标签解析成 (显示名, 是否预习单元)。预习单元不占 Unit 序号。"""
        text = (label or "").strip()
        match = re.search(r'unit\s*([0-9]+)', text, re.I)
        if match:
            return f"Unit {match.group(1)}", False
        if re.search(r'preview|intro|预备|预习|准备|starter|start\s*up', text, re.I):
            return f"预备单元 · {text}", True
        if not re.search(r'\d', text):
            return f"预备单元 · {text}", True
        return text or "其他单元", False

    @staticmethod
    def _status_key(text: str) -> str:
        value = (text or "").strip()
        if any(kw in value for kw in ("进行中", "未完成", "继续", "待批阅", "待评分")):
            return "doing"
        if any(kw in value for kw in ("未开始", "未学习", "未做")):
            return "todo"
        if any(kw in value for kw in ("已完成", "完成", "通过", "满分")):
            return "done"
        return "unknown"

    def _scan_course_directory(self, current_unit_only: bool = False):
        tabs = []
        try:
            player_unit = ''
            if self.solver._is_publish_page():
                route = self.driver.current_url
                resource = parse_qs(urlsplit(route).query).get('courseResourceId', [''])[0]
                if not resource.isdigit():
                    raise ValueError('播放器地址缺少 courseResourceId，无法定位课程目录')
                match = re.search(r'/courseware/([^/]+)', urlsplit(route).fragment)
                player_unit = match.group(1) if match else ''
                if current_unit_only and not player_unit:
                    raise ValueError('播放器地址缺少单元标识，无法确认当前单元')
                self.gui_log_queue.put('从播放器返回本教材课程目录，读取任务列表')
                self.driver.get(f'https://ucloud.unipus.cn/app/cmgt/resource-detail/{resource}')
            unit_container = self.WebDriverWait(self.driver, 5).until(
                self.EC.presence_of_element_located((self.By.CSS_SELECTOR, '[class*="unitTabScrollContainer"]'))
            )
            unit_tabs = unit_container.find_elements(self.By.CSS_SELECTOR, ":scope > *")
            self.gui_log_queue.put(f"课程目录页，找到 {len(unit_tabs)} 个 Unit")

            unit_infos = []
            for unit_idx, unit_tab in enumerate(unit_tabs):
                try:
                    label = self.driver.execute_script(self.UNIT_LABEL_JS, unit_tab) or ""
                except Exception:
                    label = (unit_tab.text or "").strip()
                display_name, is_preview = self._parse_unit_label(label)
                unit_id = re.fullmatch(r'id-(.+)-tab', unit_tab.get_attribute('id') or '')
                unit_infos.append({
                    "index": unit_idx,
                    "label": label,
                    "display": display_name,
                    "is_preview": is_preview,
                    "element": unit_tab,
                    "unit_id": unit_id.group(1) if unit_id else '',
                })

            active_info = next((i for i in unit_infos if self._is_active_unit(i["element"])), None)
            if current_unit_only and player_unit:
                active_info = next((i for i in unit_infos
                                    if i['element'].get_attribute('id') == f'id-{player_unit}-tab'), None)
                if active_info is None:
                    raise ValueError('课程目录中找不到播放器当前单元')
            directory_url = self.driver.current_url

            def collect_unit(info, click_tab: bool):
                name_counts = {}
                if click_tab:
                    try:
                        self.driver.execute_script("arguments[0].click();", info["element"])
                    except Exception:
                        try:
                            info["element"].click()
                        except Exception:
                            return
                    time.sleep(0.9)
                tasks = self.driver.execute_script(self.READ_TASKS_JS) or []
                elements = self.driver.find_elements(self.By.CSS_SELECTOR, self.TASK_ITEM_SELECTOR)
                for task in tasks:
                    name = (task.get("name") or "").strip()
                    if not name:
                        continue
                    element = None
                    dom_index = task.get("index")
                    if isinstance(dom_index, int) and 0 <= dom_index < len(elements):
                        try:
                            element = elements[dom_index].find_element(
                                self.By.CSS_SELECTOR,
                                '[class*="taskTypeName"], [class*="nodeTypeName"]')
                        except Exception:
                            element = elements[dom_index]
                    section_title = (task.get("section") or "").strip()
                    occurrence = name_counts.get(name, 0)
                    name_counts[name] = occurrence + 1
                    status_key = self._status_key(task.get("status", ""))
                    compulsory = bool(task.get("compulsory"))
                    tabs.append({
                        "l1_idx": len(tabs),
                        "l2_idx": -1,
                        "l1_title": name,
                        "l2_title": section_title,
                        "display": self._format_task_display(
                            compulsory, info["display"], section_title, name, task.get("status", "")),
                        "is_l2": False,
                        "is_compulsory": compulsory,
                        "unit_label": info["display"],
                        "unit_is_preview": info["is_preview"],
                        "section": section_title,
                        "status": status_key,
                        "status_text": (task.get("status") or "").strip(),
                        "_element": element,
                        "_unit_idx": info["index"],
                        "_unit_id": info.get("unit_id", ""),
                        "_section_title": section_title,
                        "_name_occurrence": occurrence,
                        "_directory_url": directory_url,
                    })

            if current_unit_only:
                # 只读「当前已经打开」的那个单元：不点任何 tab，直接抓现行 DOM
                info = active_info or {
                    "index": 0,
                    "label": "",
                    "display": "当前单元",
                    "is_preview": False,
                }
                self.gui_log_queue.put(f"仅扫描当前单元: {info['display']}（不切换其它单元）")
                collect_unit(info, click_tab=bool(player_unit))
            else:
                for info in unit_infos:
                    collect_unit(info, click_tab=True)
        except Exception as e:
            if player_unit:
                self.gui_log_queue.put(f'播放器任务扫描失败: {str(e)[:100]}')
            if self.logger:
                self.logger.debug(f"课程目录扫描未命中: {e}")
        if tabs:
            self._enrich_task_grading(tabs)
        return tabs

    def _enrich_task_grading(self, tabs):
        """读取学习记录，不切换目录单元，也不触发任务学习或提交。"""
        index = {}
        try:
            route = urlsplit(self.driver.current_url)
            resource = re.fullmatch(r'/app/cmgt/resource-detail/(\d+)/?', route.path)
            unit_ids = list(dict.fromkeys(t['_unit_id'] for t in tabs if t.get('_unit_id')))
            if route.hostname == 'ucloud.unipus.cn' and resource and unit_ids:
                self.gui_log_queue.put('正在读取学习记录中的计分标记...')
                result = self.driver.execute_async_script(READ_GRADING_JS, resource.group(1), unit_ids) or {}
                index = build_record_index(result.get('units') or [])
        except Exception:
            # 不让记录接口失败阻断目录扫描，且不在日志中暴露响应或登录凭据。
            self.gui_log_queue.put('学习记录计分标记读取失败，保留任务并标记为计分未知')
        assign_task_records(tabs, index)
        for tab in tabs:
            tab['display'] += f" · {grading_text(tab['is_scored'])}"
            if tab.get('score') is not None:
                tab['display'] += f" · 已有 {score_text(tab['score'])} 分"
        ungraded = sum(t['is_scored'] is False for t in tabs)
        unknown = sum(t['is_scored'] is None for t in tabs)
        graded = sum(t.get('score') is not None for t in tabs)
        self.gui_log_queue.put(
            f'计分标记：计分 {len(tabs) - ungraded - unknown}，不计分 {ungraded}，未知 {unknown}'
            + ('；未知任务不会被排除，请按需检查学习记录' if unknown else ''))
        if graded:
            self.gui_log_queue.put(f'学习记录里已有 {graded} 个任务带分数')
        unmatched = sum(not t['_record_matched'] for t in tabs)
        if unmatched:
            self.gui_log_queue.put(f'{unmatched} 项未匹配到学习记录，请核对平台目录或课程结构')

    @staticmethod
    def _format_task_display(compulsory, unit_name, section, name, status_text):
        prefix = "[必修]" if compulsory else "[选修]"
        # 目录里的折叠标题经常重复（Course preview / Course preview），去重后更易读
        section_parts = [part.strip() for part in (section or "").split("/") if part.strip()]
        section = " / ".join(dict.fromkeys(section_parts))
        if section.strip().lower() == (name or "").strip().lower():
            section = ""
        parts = [prefix, unit_name]
        if section:
            parts.append(section)
        parts.append(name)
        display = " · ".join(parts)
        if status_text:
            display += f" · {status_text}"
        return display

    def _is_active_unit(self, element) -> bool:
        try:
            return bool(self.driver.execute_script(self.ACTIVE_UNIT_JS, element))
        except Exception:
            return False

    def refresh_units_from_server(self, directory_url: str, unit_indices) -> list:
        """返回课程目录并向平台核验指定单元的真实任务完成状态与分数，不信任本地推测。"""
        if not directory_url or not unit_indices:
            return []
        wanted = sorted({int(i) for i in unit_indices if i is not None})
        if not wanted:
            return []
        refreshed = []
        try:
            if self.driver.current_url != directory_url:
                self.driver.get(directory_url)
            else:
                self.driver.refresh()
            unit_container = self.WebDriverWait(self.driver, 12).until(
                self.EC.presence_of_element_located(
                    (self.By.CSS_SELECTOR, '[class*="unitTabScrollContainer"]'))
            )
            unit_tabs = unit_container.find_elements(self.By.CSS_SELECTOR, ":scope > *")
            for unit_idx in wanted:
                if unit_idx < 0 or unit_idx >= len(unit_tabs):
                    continue
                unit_tab = unit_tabs[unit_idx]
                if not self._is_active_unit(unit_tab):
                    self.driver.execute_script("arguments[0].click();", unit_tab)
                    time.sleep(0.9)
                else:
                    time.sleep(0.35)
                try:
                    label = self.driver.execute_script(self.UNIT_LABEL_JS, unit_tab) or ""
                except Exception:
                    label = (unit_tab.text or "").strip()
                display_name, _is_preview = self._parse_unit_label(label)
                match = re.fullmatch(r'id-(.+)-tab', unit_tab.get_attribute('id') or '')
                unit_id = match.group(1) if match else ''
                try:
                    tasks = self.WebDriverWait(self.driver, 8).until(
                        lambda driver: self._is_active_unit(unit_tab)
                        and driver.execute_script(self.READ_TASKS_JS))
                except TimeoutException:
                    self.gui_log_queue.put(f'{display_name} 未读取到已加载的目录任务')
                    continue
                name_counts = {}
                for task in tasks:
                    name = (task.get("name") or "").strip()
                    if not name:
                        continue
                    section_title = (task.get("section") or "").strip()
                    occ = name_counts.get(name, 0)
                    name_counts[name] = occ + 1
                    refreshed.append({
                        "l1_title": name,
                        "section": section_title,
                        "status": self._status_key(task.get("status", "")),
                        "status_text": (task.get("status") or "").strip(),
                        "_unit_idx": unit_idx,
                        "_unit_id": unit_id,
                        "_name_occurrence": occ,
                        "display": self._format_task_display(
                            bool(task.get("compulsory")), display_name,
                            section_title, name, task.get("status", "")),
                    })
            if refreshed:
                self._enrich_task_grading(refreshed)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"平台任务状态复核失败: {exc}")
            return []
        return refreshed
