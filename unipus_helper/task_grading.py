"""Read the portal's explicit scoring flags without opening or submitting tasks."""

import re
from collections import Counter, defaultdict


MIN_TASK_SCORE = 85


# Keep credentials in the browser. Only the requested units' record trees leave it.
READ_GRADING_JS = """
    const resource = arguments[0], units = arguments[1];
    const done = arguments[arguments.length - 1];
    if (location.origin !== 'https://ucloud.unipus.cn' || !/^\\d+$/.test(resource)) {
        done({units: [], failed: units.length});
        return;
    }
    const read = key => {
        try { return JSON.parse(localStorage.getItem(key) || '{}') || {}; }
        catch (_) { return {}; }
    };
    const legacy = read('__token'), portal = read('PORTAL_STATE_PERSISTENT');
    const jwt = legacy.jwt || portal.Authorization || (portal.tokenInfo || {}).jwt;
    const headers = jwt ? {Authorization: jwt} : {};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    const get = async path => {
        const response = await fetch(path, {
            headers, credentials: 'same-origin', signal: controller.signal
        });
        if (!response.ok) throw new Error('records unavailable');
        const body = await response.json();
        if (body.code !== 1 || body.success === false || !body.value)
            throw new Error('records unavailable');
        return body.value;
    };
    (async () => {
        try {
            const {userInfo: user} = await get('/api/account/user/info');
            if (!user || !user.appUserId || !user.ssoId) throw new Error('no user');
            const results = await Promise.all(units.map(async unit => {
                try {
                    const query = new URLSearchParams({
                        id: resource, nodeId: unit,
                        appUserId: user.appUserId, ssoId: user.ssoId
                    });
                    const value = await get(
                        '/api/tla/learningDetail/studyRecord/unitTaskSituation?' + query);
                    if (!Array.isArray(value.list)) throw new Error('no tree');
                    return {unit_id: unit, tree: value.list};
                } catch (_) { return null; }
            }));
            done({units: results.filter(Boolean), failed: results.filter(x => !x).length});
        } catch (_) {
            done({units: [], failed: units.length});
        } finally { clearTimeout(timer); }
    })();
"""


def _clean(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().casefold()


def _path(section):
    return tuple(_clean(part) for part in section.split('/') if _clean(part))


def _node_name(node):
    return ' '.join(str(node.get(k) or '').strip() for k in ('caption', 'name')).strip()


def _leaf_record(node):
    flag = node.get('scoreTaskFlag')
    return {
        'scored': flag if type(flag) is bool else None,
        'score': confirmed_score(node.get('score')) if flag is not False else None,
        'progress': confirmed_score(node.get('finishProgress')),
    }


def build_record_index(units):
    """Index unique full task paths; repeated names alone cannot identify a task."""
    candidates = defaultdict(list)

    def visit(node, unit_id, parents, route):
        if not isinstance(node, dict):
            return
        name = _node_name(node)
        children = node.get('children')
        path = node.get('path')
        route = route + (path,) if route and isinstance(path, str) and path else ()
        key = (unit_id, tuple(_clean(p) for p in parents), _clean(name))
        if isinstance(children, list) and children:
            # 新版目录的一项对应父节点，成绩和完成度由其下的题组、视频、正文等分别记录。
            if name and node.get('role') == 'node' and all(
                    isinstance(child, dict) and child.get('role') == 'group' for child in children):
                groups = [
                    dict(_leaf_record(child), name=_node_name(child), kind=child.get('tabType'),
                         required=child.get('required') is not False)
                    for child in children
                ]
                graded = [group for group in groups if group['scored'] is True]
                scored = True if graded else False if all(g['scored'] is False for g in groups) else None
                candidates[key].append({
                    'scored': scored,
                    'score': graded[0]['score'] if len(graded) == 1 else None,
                    'progress': None,
                    'groups': groups,
                    'route': route,
                })
            for child in children:
                visit(child, unit_id, parents + ([name] if name else []), route)
        elif node.get('role') not in ('unit', 'section', 'node') and name:
            candidates[key].append(_leaf_record(node))

    for unit in units:
        unit_id = unit.get('unit_id')
        if not unit_id:
            continue
        for root in unit.get('tree', []):
            # Ignore data for other units, even if names and sections coincide.
            if isinstance(root, dict) and root.get('nodeId') == unit_id and root.get('role') == 'unit':
                path = root.get('path')
                route = (path,) if isinstance(path, str) and path else ()
                for child in root.get('children', []):
                    visit(child, unit_id, [], route)
    return {key: records[0] if len(records) == 1 else None
            for key, records in candidates.items()}


def build_grading_index(units):
    return {key: record['scored'] if record else None
            for key, record in build_record_index(units).items()}


def confirmed_score(value):
    return float(value) if type(value) in (int, float) and 0 <= value <= 100 else None


def score_text(score):
    value = confirmed_score(score)
    return '' if value is None else str(value).removesuffix('.0')


def task_scoring(index, unit_id, section, name):
    return index.get((unit_id, _path(section), _clean(name)))


def _task_keys(tasks):
    keys = [(t.get('_unit_id'), _path(t.get('section') or ''), _clean(t.get('l1_title')))
            for t in tasks]
    return keys, Counter(keys)


def assign_task_scoring(tasks, index):
    keys, counts = _task_keys(tasks)
    for task, key in zip(tasks, keys):
        task['is_scored'] = index.get(key) if key[0] and counts[key] == 1 else None


def assign_task_records(tasks, index):
    """把学习记录里的计分标记、已有分数、完成度写回任务。"""
    keys, counts = _task_keys(tasks)
    for task, key in zip(tasks, keys):
        record = index.get(key) if key[0] and counts[key] == 1 else None
        task['_record_matched'] = record is not None
        task['is_scored'] = record['scored'] if record else None
        task['score'] = record['score'] if record else None
        task['progress'] = record['progress'] if record else None
        task['_record_groups'] = record.get('groups', []) if record else []
        task['_record_route'] = record.get('route', ()) if record else ()


def grading_text(scored):
    return '计分' if scored is True else '不计分' if scored is False else '计分未知'


def needs_live_conversation(task):
    title = task.get('l2_title') if task.get('is_l2') else task.get('l1_title')
    return re.fullmatch(r'talk with ai(?: \d+)?', _clean(title)) is not None


def selectable_task(task, exclude_ungraded=False):
    return (not needs_live_conversation(task)
            and not (exclude_ungraded and task.get('is_scored') is False))
