"""自定义 API 插件 — 通过 Web 面板配置正则触发的自定义 HTTP API 调用

适配自 miaolik/ElainaBot_v2plugins 的旧版「自定义api.web.py」。
旧版基于 v1 的 PluginManager.get_regex_handlers (启动时动态注册) + reload_plugin 热重载;
本版改用 v2 的装饰器在导入时按配置动态注册 @handler, 配置变更后调用
plugin_manager.reload('custom_api') 重新导入即重新注册。Web 管理面板改用
register_page / register_route 实现。
"""

import asyncio
import base64
import datetime
import json
import mimetypes
import os
import re
import time
import urllib.parse
import uuid

import aiohttp
import requests
from aiohttp import web

from core.base.logger import PLUGIN, get_logger, report_error
from core.message._http import MessageType
from core.message._media_send import _set_msg_or_event_id
from core.message.keyboard import convert_simple_ark_data
from core.message.media import upload_media_bytes
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page

__plugin_meta__ = {
    'name': '自定义API',
    'author': 'miaolik',
    'description': '通过 Web 面板配置正则触发的自定义 API 调用 (支持多种返回类型)',
    'version': '2.0.0',
    'github': 'https://github.com/miaolik/ElainaBot_v2plugins',
    'license': 'MIT',
}

log = get_logger(PLUGIN, '自定义API')

try:
    import brotli  # noqa: F401
    _HAS_BROTLI = True
except ImportError:
    try:
        import brotlicffi  # noqa: F401
        _HAS_BROTLI = True
    except ImportError:
        _HAS_BROTLI = False


def _sanitize_accept_encoding(headers):
    """无 brotli 库时, 从 Accept-Encoding 中剔除 br, 否则服务器返回的 brotli 响应
    会被 requests 解码失败, 导致 response.json()/text 抛 JSONDecodeError。"""
    if _HAS_BROTLI:
        return headers
    for key in list(headers):
        if key.lower() == 'accept-encoding':
            encodings = [e.strip() for e in str(headers[key]).split(',')]
            encodings = [e for e in encodings if e and e.split(';')[0].strip().lower() != 'br']
            if encodings:
                headers[key] = ', '.join(encodings)
            else:
                del headers[key]
    return headers

_PLUGIN_DIR = os.path.dirname(__file__)
_PAGE_FILE = os.path.join(_PLUGIN_DIR, 'page.html')
_DATA_DIR = os.path.join(_PLUGIN_DIR, 'data', 'custom_api')
_CONFIG_FILE = os.path.join(_DATA_DIR, 'api_config.json')
_TEMP_DIR = os.path.join(_DATA_DIR, 'temp')

_DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Cache-Control': 'no-cache',
    'Upgrade-Insecure-Requests': '1',
}


# ==================== 配置持久化 ====================

def _load_config():
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, encoding='utf-8') as f:
                return json.load(f)
        with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'apis': []}, f, ensure_ascii=False, indent=2)
        return {'apis': []}
    except Exception as e:
        log.warning(f'加载配置失败: {e}')
        return {'apis': []}


def _save_config(config):
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.warning(f'保存配置失败: {e}')
        return False


async def _reload_self():
    """配置变更后热重载本插件, 以便按新配置重新注册 @handler。
    框架按插件目录名重载, 目录名可能不是 custom_api, 故取本文件所在目录名。"""
    try:
        from core.application import get_app

        app = get_app()
        if app and app.plugin_manager:
            await app.plugin_manager.reload(os.path.basename(_PLUGIN_DIR))
    except Exception as e:
        log.warning(f'热重载失败: {e}')


# ==================== 60s API 预设库 ====================
# 项目: https://github.com/vikiboss/60s  文档: https://docs.60s-api.viki.moe/
# 公共实例开箱即用, 无需本地部署; 也可自行部署后在 Web 面板修改基础地址。

_SIXTY_DEFAULT_BASE = 'https://60s.viki.moe'
_SIXTY_ID_PREFIX = 'sixty_'

# 每项: key(唯一标识), name, desc, regex(触发正则), path(接口路径), params(默认参数,
# 支持 {$1} 等变量), 可选 response_type/reply_type/message_template (默认 text/text/无)
_SIXTY_PRESETS = [
    {'key': '60s', 'name': '每天60s读懂世界', 'desc': '每日精选15条新闻+微语', 'regex': '^(60s|每日新闻|读懂世界)$', 'path': '/v2/60s', 'params': {'encoding': 'text'}},
    {'key': '60s_image', 'name': '60s新闻图', 'desc': '每日新闻长图', 'regex': '^(60s图|新闻图)$', 'path': '/v2/60s', 'params': {}, 'response_type': 'json', 'reply_type': 'image', 'message_template': '{data.image}'},
    {'key': 'bing', 'name': '必应每日壁纸', 'desc': 'Bing 每日壁纸大图', 'regex': '^(必应壁纸|bing壁纸)$', 'path': '/v2/bing', 'params': {}, 'response_type': 'json', 'reply_type': 'image', 'message_template': '{data.cover}'},
    {'key': 'hitokoto', 'name': '一言', 'desc': '随机一句话文案', 'regex': '^一言$', 'path': '/v2/hitokoto', 'params': {'encoding': 'text'}},
    {'key': 'duanzi', 'name': '随机段子', 'desc': '随机搞笑段子', 'regex': '^(段子|讲个段子)$', 'path': '/v2/duanzi', 'params': {'encoding': 'text'}},
    {'key': 'kfc', 'name': '疯狂星期四', 'desc': 'KFC v50 文案', 'regex': '^(kfc|疯狂星期四|v50)$', 'path': '/v2/kfc', 'params': {'encoding': 'text'}},
    {'key': 'fabing', 'name': '发病文学', 'desc': '发病 对象名 (可省略)', 'regex': '^发病\\s*(.*)$', 'path': '/v2/fabing', 'params': {'encoding': 'text', 'name': '{$1}'}},
    {'key': 'answer', 'name': '答案之书', 'desc': '随机一条人生答案', 'regex': '^(答案之书|答案)$', 'path': '/v2/answer', 'params': {'encoding': 'text'}},
    {'key': 'luck', 'name': '今日运势', 'desc': '随机运势签文', 'regex': '^(今日运势|运势)$', 'path': '/v2/luck', 'params': {'encoding': 'text'}},
    {'key': 'dad_joke', 'name': '冷笑话', 'desc': '随机冷笑话', 'regex': '^冷笑话$', 'path': '/v2/dad-joke', 'params': {'encoding': 'text'}},
    {'key': 'weibo', 'name': '微博热搜', 'desc': '微博实时热搜榜', 'regex': '^微博热搜$', 'path': '/v2/weibo', 'params': {'encoding': 'text'}},
    {'key': 'zhihu', 'name': '知乎热榜', 'desc': '知乎实时热榜', 'regex': '^知乎热榜$', 'path': '/v2/zhihu', 'params': {'encoding': 'text'}},
    {'key': 'douyin', 'name': '抖音热搜', 'desc': '抖音实时热搜榜', 'regex': '^抖音热搜$', 'path': '/v2/douyin', 'params': {'encoding': 'text'}},
    {'key': 'toutiao', 'name': '头条热搜', 'desc': '今日头条热搜榜', 'regex': '^(头条热搜|今日头条)$', 'path': '/v2/toutiao', 'params': {'encoding': 'text'}},
    {'key': 'baidu_hot', 'name': '百度热搜', 'desc': '百度实时热搜榜', 'regex': '^百度热搜$', 'path': '/v2/baidu/hot', 'params': {'encoding': 'text'}},
    {'key': 'hacker_news', 'name': 'HackerNews', 'desc': 'HackerNews 热帖', 'regex': '^(hn|hackernews)$', 'path': '/v2/hacker-news/top', 'params': {'encoding': 'text'}},
    {'key': 'it_news', 'name': 'IT资讯', 'desc': 'IT 科技新闻', 'regex': '^(it新闻|it资讯)$', 'path': '/v2/it-news', 'params': {'encoding': 'text'}},
    {'key': 'ai_news', 'name': 'AI新闻', 'desc': 'AI 领域新闻', 'regex': '^(ai新闻|ai资讯)$', 'path': '/v2/ai-news', 'params': {'encoding': 'text'}},
    {'key': 'history', 'name': '历史上的今天', 'desc': '当天的历史事件', 'regex': '^历史上的今天$', 'path': '/v2/today-in-history', 'params': {'encoding': 'text'}},
    {'key': 'moyu', 'name': '摸鱼日历', 'desc': '打工人摸鱼日历/假期倒计时', 'regex': '^(摸鱼|摸鱼日历)$', 'path': '/v2/moyu', 'params': {'encoding': 'text'}},
    {'key': 'epic', 'name': 'Epic免费游戏', 'desc': 'Epic 喜加一信息', 'regex': '^(epic|喜加一)$', 'path': '/v2/epic', 'params': {'encoding': 'text'}},
    {'key': 'lunar', 'name': '农历信息', 'desc': '今日农历/宜忌', 'regex': '^(农历|黄历)$', 'path': '/v2/lunar', 'params': {'encoding': 'text'}},
    {'key': 'exchange_rate', 'name': '汇率查询', 'desc': '汇率 币种 (如: 汇率 USD, 默认CNY)', 'regex': '^汇率\\s*([A-Za-z]*)$', 'path': '/v2/exchange-rate', 'params': {'encoding': 'text', 'currency': '{$1}'}},
    {'key': 'weather', 'name': '实时天气', 'desc': '天气 城市名', 'regex': '^天气\\s+(.+)$', 'path': '/v2/weather', 'params': {'encoding': 'text', 'query': '{$1}'}},
    {'key': 'weather_forecast', 'name': '天气预报', 'desc': '天气预报 城市名 (未来7天)', 'regex': '^天气预报\\s+(.+)$', 'path': '/v2/weather/forecast', 'params': {'encoding': 'text', 'query': '{$1}'}},
    {'key': 'fanyi', 'name': '翻译', 'desc': '翻译 内容 (自动识别语言)', 'regex': '^翻译\\s+(.+)$', 'path': '/v2/fanyi', 'params': {'encoding': 'text', 'text': '{$1}'}},
    {'key': 'baike', 'name': '百科查询', 'desc': '百科 词条名', 'regex': '^百科\\s+(.+)$', 'path': '/v2/baike', 'params': {'encoding': 'text', 'word': '{$1}'}},
    {'key': 'ip', 'name': 'IP查询', 'desc': 'ip查询 IP地址', 'regex': '^ip查询\\s+(.+)$', 'path': '/v2/ip', 'params': {'encoding': 'text', 'ip': '{$1}'}},
    {'key': 'whois', 'name': 'Whois查询', 'desc': 'whois 域名', 'regex': '^whois\\s+(.+)$', 'path': '/v2/whois', 'params': {'encoding': 'text', 'domain': '{$1}'}},
]


def _sixty_base_url(config=None):
    config = config or _load_config()
    return (config.get('sixty_base_url') or _SIXTY_DEFAULT_BASE).rstrip('/')


def _sixty_build_api(preset, base_url):
    """由预设生成一条标准 API 配置 (与手动添加的 API 结构一致, 后端可继续编辑)。"""
    return {
        'id': f"{_SIXTY_ID_PREFIX}{preset['key']}",
        'sixty_key': preset['key'],
        'sixty_path': preset['path'],
        'name': f"60s·{preset['name']}",
        'description': preset['desc'],
        'regex': preset['regex'],
        'url': f"{base_url}{preset['path']}",
        'method': 'GET',
        'headers': {},
        'params': dict(preset.get('params', {})),
        'body': {},
        'timeout': 15,
        'response_type': preset.get('response_type', 'text'),
        'reply_type': preset.get('reply_type', 'text'),
        'message_template': preset.get('message_template', ''),
        'enabled': True,
    }


# ==================== API 调用 ====================

def _replace_variables(text, event, regex_groups=()):
    if not isinstance(text, str):
        return text
    variables = {
        '{user_id}': getattr(event, 'user_id', '') or '',
        '{group_id}': getattr(event, 'group_id', '') or '',
        '{message}': getattr(event, 'content', '') or '',
        '{timestamp}': str(int(time.time())),
    }
    for i, group in enumerate(regex_groups, 1):
        variables[f'{{${i}}}'] = group if group else ''
    for key, value in variables.items():
        text = text.replace(key, str(value))
    return text


def _call_api(api_config, event, regex_groups=()):
    """调用外部 API (阻塞, 在 executor 中执行)。"""
    try:
        url = api_config.get('url', '')
        method = api_config.get('method', 'GET').upper()
        headers = api_config.get('headers', {})
        params = api_config.get('params', {})
        body = api_config.get('body', {})
        timeout = api_config.get('timeout', 10)
        response_type = api_config.get('response_type', 'text')

        if '@referer' in url:
            url = url.replace('@referer', '')

        url = _replace_variables(url, event, regex_groups)
        params = {k: _replace_variables(str(v), event, regex_groups) for k, v in params.items()}
        body = {k: _replace_variables(str(v), event, regex_groups) for k, v in body.items()}

        headers = dict(_DEFAULT_HEADERS) if not headers else dict(headers)
        headers = _sanitize_accept_encoding(headers)

        if method == 'GET':
            response = requests.get(url, headers=headers, params=params, timeout=timeout, allow_redirects=True)
        elif method == 'POST':
            response = requests.post(url, headers=headers, params=params, json=body, timeout=timeout, allow_redirects=True)
        elif method == 'PUT':
            response = requests.put(url, headers=headers, params=params, json=body, timeout=timeout, allow_redirects=True)
        elif method == 'DELETE':
            response = requests.delete(url, headers=headers, params=params, timeout=timeout, allow_redirects=True)
        else:
            return {'success': False, 'error': f'不支持的请求方法: {method}'}

        if not (200 <= response.status_code < 300):
            return {'success': False, 'error': f'HTTP {response.status_code}: {response.reason}'}

        if response_type == 'json':
            return {'success': True, 'data': response.json()}
        if response_type == 'text':
            return {'success': True, 'data': response.text}
        if response_type == 'binary':
            return {'success': True, 'data': response.content}
        return {'success': False, 'error': f'不支持的响应类型: {response_type}'}

    except requests.Timeout:
        return {'success': False, 'error': 'API请求超时'}
    except requests.RequestException as e:
        return {'success': False, 'error': f'网络错误: {e}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _extract_json_path(data, path):
    try:
        result = data
        for part in path.split('.'):
            if '[' in part and ']' in part:
                key = part[:part.index('[')]
                index = int(part[part.index('[') + 1:part.index(']')])
                result = result[key][index]
            else:
                result = result[part]
        return result
    except Exception as e:
        return f'JSON路径提取失败: {e}'


def _process_message_template(template, json_data, regex_groups=()):
    result = template
    for i, group in enumerate(regex_groups, 1):
        result = result.replace(f'{{${i}}}', str(group) if group else '')
    for path in re.findall(r'\{(?!\$)([^}]+)\}', result):
        try:
            value = _extract_json_path(json_data, path.strip())
            result = result.replace(f'{{{path}}}', str(value))
        except Exception:
            result = result.replace(f'{{{path}}}', f'[提取失败:{path}]')
    return result


def _parse_params_from_template(template_str):
    """解析支持嵌套数组的参数串: "a,b,(c,d)" -> ["a","b",["c","d"]]"""
    if not template_str:
        return []
    params = []
    current = ''
    depth = 0
    array_items = []
    for char in template_str:
        if char == '(' and depth == 0:
            if current.strip():
                params.append(current.strip())
                current = ''
            depth = 1
            array_items = []
        elif char == ')' and depth == 1:
            if current.strip():
                array_items.append(current.strip())
                current = ''
            params.append(array_items)
            depth = 0
            array_items = []
        elif char == ',' and depth == 0:
            if current.strip():
                params.append(current.strip())
            current = ''
        elif char == ',' and depth == 1:
            if current.strip():
                array_items.append(current.strip())
            current = ''
        else:
            current += char
    if current.strip():
        params.append(current.strip())
    return params


def _parse_ark_params(data):
    all_params = _parse_params_from_template(str(data))
    normal_params = []
    list_items = []
    for param in all_params:
        if isinstance(param, list):
            list_items.append(param)
        else:
            normal_params.append(param)
    if list_items:
        return normal_params + [list_items]
    return normal_params


async def _send_response(event, api_config, data, regex_groups=()):
    """根据回复类型发送响应消息。"""
    try:
        reply_type = api_config.get('reply_type', 'text')
        response_type = api_config.get('response_type', 'text')
        message_template = api_config.get('message_template', '')

        if message_template and response_type == 'json':
            data = _process_message_template(message_template, data, regex_groups)
        elif message_template and response_type == 'text':
            result = message_template
            for i, group in enumerate(regex_groups, 1):
                result = result.replace(f'{{${i}}}', str(group) if group else '')
            result = result.replace('{data}', str(data))
            data = _replace_variables(result, event, ())

        if reply_type == 'text':
            await event.reply(str(data), msg_type=MessageType.MSG_TYPE_TEXT)
        elif reply_type == 'markdown':
            await event.reply(str(data), msg_type=MessageType.MSG_TYPE_MARKDOWN)
        elif reply_type == 'template_markdown':
            await _reply_template_markdown(event, api_config, data, regex_groups)
        elif reply_type == 'image':
            image_text = _replace_variables(api_config.get('image_text', ''), event, regex_groups)
            await event.reply_image(str(data), image_text)
        elif reply_type == 'voice':
            await event.reply_voice(str(data))
        elif reply_type == 'video':
            await event.reply_video(str(data))
        elif reply_type == 'ark':
            try:
                ark_type = int(api_config.get('ark_type', 23))
            except (ValueError, TypeError):
                ark_type = 23
            params = _parse_ark_params(data)
            await event.reply_ark(ark_type, tuple(params))
        else:
            await event.reply(f'不支持的回复类型: {reply_type}')
    except Exception as e:
        report_error(PLUGIN, '自定义API', e)
        await event.reply(f'发送响应失败: {e}')


async def _reply_template_markdown(event, api_config, data, regex_groups):
    """发送 QQ 原生 Markdown 模板消息 (被动回复)。"""
    params = _parse_params_from_template(str(data))
    payload = {
        'msg_type': MessageType.MSG_TYPE_MARKDOWN,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'markdown': {
            'custom_template_id': str(api_config.get('markdown_template', '1')),
            'params': [{'key': f'text{i + 1}', 'values': [str(p)]} for i, p in enumerate(params)],
        },
    }
    keyboard_id = (api_config.get('keyboard_id') or '').strip()
    if keyboard_id:
        payload['keyboard'] = {'id': keyboard_id}
    _set_msg_or_event_id(payload, event)
    sender = event.sender
    endpoint = event.reply_endpoint
    if sender and endpoint:
        await sender.post_json(endpoint, payload)


async def _handle_api_request(event, match, api_config):
    """处理单条 API 请求 (由动态注册的 handler 调用)。"""
    try:
        regex_groups = match.groups() if match else ()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _call_api, api_config, event, regex_groups)
        if result['success']:
            await _send_response(event, api_config, result['data'], regex_groups)
        else:
            await event.reply(f"API调用失败: {result['error']}")
    except Exception as e:
        report_error(PLUGIN, '自定义API', e)
        await event.reply(f'处理请求时出错: {e}')


# ==================== 定时推送 (Cron) ====================

class _PushEvent:
    """用于定时推送场景的伪事件, 复用 _replace_variables / 模板渲染逻辑
    (无触发消息, 故 user_id / content 为空, group_id 为目标群)。"""

    def __init__(self, group_id=''):
        self.user_id = ''
        self.group_id = group_id or ''
        self.content = ''


def _parse_cron_field(field, lo, hi):
    """解析单个 cron 字段, 返回允许值集合。支持 * , - / 语法。"""
    values = set()
    for part in field.split(','):
        part = part.strip()
        if not part:
            continue
        step = 1
        rng = part
        if '/' in part:
            rng, step_str = part.split('/', 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f'步长无效: {part}')
        if rng == '*':
            start, end = lo, hi
        elif '-' in rng:
            a, b = rng.split('-', 1)
            start, end = int(a), int(b)
        else:
            start = end = int(rng)
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                values.add(v)
    return values


def _cron_match(expr, dt):
    """判断时间 dt 是否匹配标准 5 段 cron 表达式 (分 时 日 月 周)。"""
    if not expr or not isinstance(expr, str):
        return False
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    try:
        if dt.minute not in _parse_cron_field(minute, 0, 59):
            return False
        if dt.hour not in _parse_cron_field(hour, 0, 23):
            return False
        if dt.month not in _parse_cron_field(month, 1, 12):
            return False
        # cron 周: 0/7=周日 .. 6=周六; Python weekday(): 周一=0..周日=6
        cron_dow = (dt.weekday() + 1) % 7
        dom_set = _parse_cron_field(dom, 1, 31)
        dow_set = _parse_cron_field(dow, 0, 7)
        if 7 in dow_set:
            dow_set.add(0)
        dom_restricted = dom.strip() != '*'
        dow_restricted = dow.strip() != '*'
        if dom_restricted and dow_restricted:
            return dt.day in dom_set or cron_dow in dow_set
        if dom_restricted:
            return dt.day in dom_set
        if dow_restricted:
            return cron_dow in dow_set
        return True
    except (ValueError, TypeError):
        return False


def _normalize_group_ids(api):
    """从配置中提取群号列表 (兼容字符串 / 列表)。"""
    raw = api.get('cron_group_ids', [])
    if isinstance(raw, str):
        raw = re.split(r'[,\n，\s]+', raw)
    result = []
    for gid in raw or []:
        gid = str(gid).strip()
        if gid and gid not in result:
            result.append(gid)
    return result


def _get_sender(appid=''):
    """从 BotManager 获取一个 sender (用于主动消息)。"""
    try:
        from core.bot.manager import _bot_manager_ref

        if not _bot_manager_ref:
            return None
        bots = _bot_manager_ref._bots
        if not bots:
            return None
        appid = (appid or '').strip()
        if appid and appid in bots:
            return bots[appid].sender
        return next(iter(bots.values())).sender
    except Exception as e:
        log.warning(f'获取 sender 失败: {e}')
        return None


def _render_push_data(api_config, data, event, regex_groups=()):
    """渲染推送消息内容 (与被动回复 _send_response 保持一致的模板处理)。"""
    response_type = api_config.get('response_type', 'text')
    message_template = api_config.get('message_template', '')
    if message_template and response_type == 'json':
        return _process_message_template(message_template, data, regex_groups)
    if message_template and response_type == 'text':
        result = message_template
        for i, group in enumerate(regex_groups, 1):
            result = result.replace(f'{{${i}}}', str(group) if group else '')
        result = result.replace('{data}', str(data))
        return _replace_variables(result, event, ())
    return data


# 媒体下载上限 (与框架 _MAX_MEDIA_DOWNLOAD 对齐), 防止 OOM
_MAX_MEDIA_DOWNLOAD = 100 * 1024 * 1024

# 部分 jsDelivr 镜像有防盗链/区域限制, 服务端直接下载会 403; 失败时回退到官方镜像。
_JSDELIVR_FALLBACK_HOSTS = ('cdn.jsdelivr.net', 'fastly.jsdelivr.net', 'gcore.jsdelivr.net')
_JSDELIVR_PATH_PREFIXES = ('/gh/', '/npm/', '/wp/', '/combine/', '/hg/')


def _media_url_candidates(url):
    """返回可尝试下载的 URL 列表: 原始 URL + (jsDelivr 路径时) 官方镜像回退。"""
    candidates = [url]
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return candidates
    if (parsed.path or '').startswith(_JSDELIVR_PATH_PREFIXES):
        for host in _JSDELIVR_FALLBACK_HOSTS:
            if host == parsed.netloc:
                continue
            alt = urllib.parse.urlunparse(parsed._replace(netloc=host))
            if alt not in candidates:
                candidates.append(alt)
    return candidates


async def _download_media_bytes(url):
    """下载媒体为 bytes (URL 直传失败时的回退)。超过上限或全部失败返回 None。
    带浏览器 UA; jsDelivr 镜像 403 时自动回退到官方镜像。"""
    headers = {'User-Agent': _DEFAULT_HEADERS['User-Agent']}
    timeout = aiohttp.ClientTimeout(total=60)
    candidates = _media_url_candidates(url)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for candidate in candidates:
            try:
                async with session.get(candidate, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning(f'下载媒体失败: HTTP {resp.status} ({candidate})')
                        continue
                    cl = int(resp.headers.get('Content-Length', 0) or 0)
                    if cl > _MAX_MEDIA_DOWNLOAD:
                        log.warning(f'媒体过大 ({cl} bytes), 跳过下载')
                        return None
                    body = await resp.read()
                    if len(body) > _MAX_MEDIA_DOWNLOAD:
                        log.warning(f'媒体实际大小超限 ({len(body)} bytes), 丢弃')
                        return None
                    if candidate != url:
                        log.info(f'镜像回退下载成功: {candidate}')
                    return body
            except Exception as e:
                log.warning(f'下载媒体失败: {e} ({candidate})')
    return None


async def _upload_media_robust(sender, group_id, data, file_type):
    """上传媒体, 返回 file_info。对齐框架被动回复的健壮逻辑:
    bytes 直接上传; URL 先尝试直传(由 QQ 下载), 失败则本地下载后按字节上传。"""
    endpoint = f'/v2/groups/{group_id}/files'
    if isinstance(data, bytes):
        return await upload_media_bytes(sender, data, file_type, endpoint)
    media = str(data)
    file_info = await upload_media_bytes(sender, media, file_type, endpoint)
    if file_info:
        return file_info
    if media.startswith(('http://', 'https://')):
        log.debug(f'URL 直传失败, 回退下载上传: {media}')
        body = await _download_media_bytes(media)
        if body:
            return await upload_media_bytes(sender, body, file_type, endpoint)
    return None


async def _push_media(sender, group_id, data, file_type, content=''):
    file_info = await _upload_media_robust(sender, group_id, data, file_type)
    if not file_info:
        log.warning(f'定时推送媒体上传失败 (group={group_id})')
        return
    payload = {
        'msg_type': MessageType.MSG_TYPE_MEDIA,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'content': content or '',
        'media': {'file_info': file_info},
    }
    await sender.post_json(f'/v2/groups/{group_id}/messages', payload)


async def _push_ark(sender, group_id, template_id, kv_data):
    if isinstance(kv_data, tuple | list) and template_id in (23, 24, 37):
        kv_data = convert_simple_ark_data(template_id, kv_data)
    payload = {
        'msg_type': MessageType.MSG_TYPE_ARK,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'content': '',
        'ark': {'template_id': template_id, 'kv': kv_data},
    }
    await sender.post_json(f'/v2/groups/{group_id}/messages', payload)


async def _push_template_markdown(sender, group_id, api_config, data, regex_groups):
    params = _parse_params_from_template(str(data))
    payload = {
        'msg_type': MessageType.MSG_TYPE_MARKDOWN,
        'msg_seq': int(time.time() * 1000) % 1000000,
        'markdown': {
            'custom_template_id': str(api_config.get('markdown_template', '1')),
            'params': [{'key': f'text{i + 1}', 'values': [str(p)]} for i, p in enumerate(params)],
        },
    }
    keyboard_id = (api_config.get('keyboard_id') or '').strip()
    if keyboard_id:
        payload['keyboard'] = {'id': keyboard_id}
    await sender.post_json(f'/v2/groups/{group_id}/messages', payload)


async def _push_to_group(sender, group_id, api_config, raw_data, regex_groups=()):
    """根据回复类型主动推送到单个群。"""
    event = _PushEvent(group_id)
    reply_type = api_config.get('reply_type', 'text')
    data = _render_push_data(api_config, raw_data, event, regex_groups)

    if reply_type == 'text':
        await sender.send_to_group(group_id, str(data), msg_type=MessageType.MSG_TYPE_TEXT)
    elif reply_type == 'markdown':
        await sender.send_to_group(group_id, str(data), msg_type=MessageType.MSG_TYPE_MARKDOWN)
    elif reply_type == 'template_markdown':
        await _push_template_markdown(sender, group_id, api_config, data, regex_groups)
    elif reply_type == 'image':
        image_text = _replace_variables(api_config.get('image_text', ''), event, regex_groups)
        await _push_media(sender, group_id, data, 1, image_text)
    elif reply_type == 'voice':
        await _push_media(sender, group_id, data, 3)
    elif reply_type == 'video':
        await _push_media(sender, group_id, data, 2)
    elif reply_type == 'ark':
        try:
            ark_type = int(api_config.get('ark_type', 23))
        except (ValueError, TypeError):
            ark_type = 23
        await _push_ark(sender, group_id, ark_type, tuple(_parse_ark_params(data)))
    else:
        await sender.send_to_group(group_id, str(data))


async def _run_scheduled_api(api):
    """调用 API 并将结果推送到配置的所有群。"""
    group_ids = _normalize_group_ids(api)
    if not group_ids:
        log.warning(f"定时推送 [{api.get('name', api.get('id', '?'))}] 未配置群号, 跳过")
        return
    sender = _get_sender(api.get('cron_appid', ''))
    if not sender:
        log.warning('定时推送无可用机器人 (sender 为空), 跳过')
        return
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _call_api, api, _PushEvent(), ())
    if not result.get('success'):
        log.warning(f"定时推送调用 API 失败: {result.get('error')}")
        return
    for group_id in group_ids:
        try:
            await _push_to_group(sender, group_id, api, result['data'], ())
        except Exception as e:
            report_error(PLUGIN, '自定义API', e)
            log.warning(f'定时推送到群 {group_id} 失败: {e}')


async def _run_due_tasks(now):
    for api in _load_config().get('apis', []):
        if not api.get('cron_enabled', False):
            continue
        if _cron_match(api.get('cron_expr', ''), now):
            try:
                await _run_scheduled_api(api)
            except Exception as e:
                report_error(PLUGIN, '自定义API', e)


async def _scheduler_loop():
    """对齐整分钟轮询, 每分钟检查一次到期的定时推送任务。"""
    try:
        while True:
            now = datetime.datetime.now()
            sleep_secs = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(max(sleep_secs, 1))
            await _run_due_tasks(datetime.datetime.now())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        report_error(PLUGIN, '自定义API', e)


_SCHEDULER_TASK_NAME = 'custom_api_scheduler'
_scheduler_task = None


def _cancel_existing_schedulers():
    """取消所有残留的调度器任务, 防止热重载导致多个调度器并存而重复推送。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for task in asyncio.all_tasks(loop):
        if task.get_name() == _SCHEDULER_TASK_NAME and not task.done():
            task.cancel()


@on_load
async def _start_scheduler():
    global _scheduler_task
    _cancel_existing_schedulers()
    _scheduler_task = asyncio.create_task(_scheduler_loop(), name=_SCHEDULER_TASK_NAME)
    log.info('定时推送调度器已启动')


@on_unload
async def _stop_scheduler():
    global _scheduler_task
    _cancel_existing_schedulers()
    _scheduler_task = None


# ==================== 动态注册处理器 ====================

def _register_api_handler(api):
    """为单个启用的 API 注册一个 @handler (闭包绑定其配置)。"""
    regex = api.get('regex', '')
    if not regex:
        return

    @handler(
        regex,
        name=f"custom_api:{api.get('id', regex)}",
        desc=api.get('description', '') or '自定义API',
        owner_only=api.get('owner_only', False),
        group_only=api.get('group_only', False),
    )
    async def _dynamic_handler(event, match, _api=api):
        await _handle_api_request(event, match, _api)


def _register_all_handlers():
    for api in _load_config().get('apis', []):
        if api.get('enabled', False):
            try:
                _register_api_handler(api)
            except re.error as e:
                log.warning(f"API [{api.get('id', '?')}] 正则无效, 跳过: {e}")


_register_all_handlers()


# ==================== Web 路由 ====================

async def _json_body(request):
    try:
        return await request.json()
    except Exception:
        return {}


@register_route('GET', '/api/ext/custom_api/list')
async def api_list_apis(request):
    config = _load_config()
    return web.json_response({'success': True, 'data': {'apis': config.get('apis', [])}})


@register_route('POST', '/api/ext/custom_api/get')
async def api_get_api(request):
    body = await _json_body(request)
    api_id = body.get('api_id')
    if not api_id:
        return web.json_response({'success': False, 'message': '缺少API ID'})
    for api in _load_config().get('apis', []):
        if api.get('id') == api_id:
            return web.json_response({'success': True, 'data': {'api': api}})
    return web.json_response({'success': False, 'message': 'API不存在'})


@register_route('POST', '/api/ext/custom_api/save')
async def api_save_api(request):
    try:
        body = await _json_body(request)
        config = _load_config()
        api_id = body.get('id')
        existing_index = next((i for i, a in enumerate(config.get('apis', [])) if a.get('id') == api_id), None)
        if existing_index is not None:
            config['apis'][existing_index] = body
        else:
            config.setdefault('apis', []).append(body)
        if _save_config(config):
            await _reload_self()
            return web.json_response({'success': True, 'message': '保存成功'})
        return web.json_response({'success': False, 'message': '保存失败'})
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)})


@register_route('POST', '/api/ext/custom_api/delete')
async def api_delete_api(request):
    body = await _json_body(request)
    api_id = body.get('api_id')
    if not api_id:
        return web.json_response({'success': False, 'message': '缺少API ID'})
    config = _load_config()
    config['apis'] = [a for a in config.get('apis', []) if a.get('id') != api_id]
    if _save_config(config):
        await _reload_self()
        return web.json_response({'success': True, 'message': '删除成功'})
    return web.json_response({'success': False, 'message': '删除失败'})


@register_route('POST', '/api/ext/custom_api/toggle')
async def api_toggle_api(request):
    body = await _json_body(request)
    api_id = body.get('api_id')
    if not api_id:
        return web.json_response({'success': False, 'message': '缺少API ID'})
    config = _load_config()
    for api in config.get('apis', []):
        if api.get('id') == api_id:
            api['enabled'] = not api.get('enabled', False)
            break
    if _save_config(config):
        await _reload_self()
        return web.json_response({'success': True, 'message': '操作成功'})
    return web.json_response({'success': False, 'message': '操作失败'})


@register_route('GET', '/api/ext/custom_api/sixty/presets')
async def api_sixty_presets(request):
    """60s 预设列表: 含每个预设是否已添加, 及当前基础地址。"""
    config = _load_config()
    added_ids = {a.get('id') for a in config.get('apis', [])}
    presets = []
    for p in _SIXTY_PRESETS:
        presets.append({
            'key': p['key'],
            'name': p['name'],
            'desc': p['desc'],
            'regex': p['regex'],
            'path': p['path'],
            'reply_type': p.get('reply_type', 'text'),
            'added': f"{_SIXTY_ID_PREFIX}{p['key']}" in added_ids,
        })
    return web.json_response({'success': True, 'data': {
        'base_url': _sixty_base_url(config),
        'default_base_url': _SIXTY_DEFAULT_BASE,
        'presets': presets,
    }})


@register_route('POST', '/api/ext/custom_api/sixty/add')
async def api_sixty_add(request):
    """添加 (或重置) 一批 60s 预设为标准 API 配置, 之后可在 API 列表中继续调整参数。"""
    body = await _json_body(request)
    keys = body.get('keys') or []
    if isinstance(keys, str):
        keys = [keys]
    presets = {p['key']: p for p in _SIXTY_PRESETS}
    invalid = [k for k in keys if k not in presets]
    if not keys or invalid:
        return web.json_response({'success': False, 'message': f'预设 key 无效: {invalid or "(空)"}'})
    config = _load_config()
    base_url = _sixty_base_url(config)
    apis = config.setdefault('apis', [])
    added, updated = [], []
    for k in keys:
        new_api = _sixty_build_api(presets[k], base_url)
        idx = next((i for i, a in enumerate(apis) if a.get('id') == new_api['id']), None)
        if idx is not None:
            apis[idx] = new_api
            updated.append(k)
        else:
            apis.append(new_api)
            added.append(k)
    if _save_config(config):
        await _reload_self()
        return web.json_response({'success': True, 'message': f'已添加 {len(added)} 个, 重置 {len(updated)} 个', 'data': {'added': added, 'updated': updated}})
    return web.json_response({'success': False, 'message': '保存失败'})


@register_route('POST', '/api/ext/custom_api/sixty/base_url')
async def api_sixty_base_url(request):
    """设置 60s 基础地址 (公共实例或自部署实例), 并同步更新所有已添加的 60s API 的 URL。"""
    body = await _json_body(request)
    base_url = str(body.get('base_url', '')).strip().rstrip('/')
    if not base_url:
        base_url = _SIXTY_DEFAULT_BASE
    if not base_url.startswith(('http://', 'https://')):
        return web.json_response({'success': False, 'message': '基础地址需以 http(s):// 开头'})
    config = _load_config()
    config['sixty_base_url'] = base_url
    updated = 0
    for api in config.get('apis', []):
        path = api.get('sixty_path')
        if str(api.get('id', '')).startswith(_SIXTY_ID_PREFIX) and path:
            api['url'] = f'{base_url}{path}'
            updated += 1
    if _save_config(config):
        await _reload_self()
        return web.json_response({'success': True, 'message': f'基础地址已更新, 同步了 {updated} 个 60s API'})
    return web.json_response({'success': False, 'message': '保存失败'})


@register_route('GET', '/api/ext/custom_api/temp', auth=False)
async def api_get_temp_file(request):
    try:
        filename = request.query.get('filename')
        if not filename or '/' in filename or '\\' in filename or '..' in filename:
            return web.json_response({'success': False, 'message': '文件名无效'})
        filepath = os.path.join(_TEMP_DIR, filename)
        if not os.path.exists(filepath):
            return web.json_response({'success': False, 'message': '文件不存在'})
        with open(filepath, 'rb') as f:
            file_data = f.read()
        mime_type = mimetypes.guess_type(filepath)[0] or 'application/octet-stream'
        return web.json_response({
            'success': True,
            'data': {
                'mime_type': mime_type,
                'base64': base64.b64encode(file_data).decode('utf-8'),
                'size': len(file_data),
            },
        })
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)})


@register_route('POST', '/api/ext/custom_api/test')
async def api_test_api(request):
    try:
        body = await _json_body(request)

        class _MockEvent:
            user_id = 'test_user'
            group_id = 'test_group'
            content = 'test message'

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _call_api, body, _MockEvent(), ())
        if not result.get('success'):
            return web.json_response({'success': False, 'message': result.get('error', '未知错误')})

        data = result.get('data')
        if isinstance(data, bytes):
            os.makedirs(_TEMP_DIR, exist_ok=True)
            file_id = str(uuid.uuid4())
            file_ext, mime_type = _sniff_binary(data)
            filename = f'{file_id}{file_ext}'
            with open(os.path.join(_TEMP_DIR, filename), 'wb') as f:
                f.write(data)
            return web.json_response({
                'success': True,
                'data': {
                    'type': 'binary',
                    'mime_type': mime_type,
                    'size': len(data),
                    'file_id': file_id,
                    'filename': filename,
                },
            })
        return web.json_response({'success': True, 'data': data})
    except Exception as e:
        report_error(PLUGIN, '自定义API', e)
        return web.json_response({'success': False, 'message': str(e)})


def _sniff_binary(data):
    if data[:2] == b'\xff\xd8':
        return '.jpg', 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png', 'image/png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif', 'image/gif'
    if data[:4] == b'RIFF' and len(data) > 12 and data[8:12] == b'WEBP':
        return '.webp', 'image/webp'
    if len(data) > 12 and data[4:8] == b'ftyp':
        return '.mp4', 'video/mp4'
    if data[:4] == b'OggS':
        return '.ogg', 'audio/ogg'
    return '.bin', 'application/octet-stream'


# ==================== 页面注册 ====================

register_page(
    key='custom_api',
    label='自定义API',
    source='plugin',
    source_name='custom_api',
    html_file=_PAGE_FILE,
    icon='link',
)


@on_unload
def _cleanup():
    unregister_page('custom_api')
