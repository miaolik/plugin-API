"""自定义 API 插件 — 通过 Web 面板配置正则触发的自定义 HTTP API 调用

适配自 miaolik/ElainaBot_v2plugins 的旧版「自定义api.web.py」。
旧版基于 v1 的 PluginManager.get_regex_handlers (启动时动态注册) + reload_plugin 热重载;
本版改用 v2 的装饰器在导入时按配置动态注册 @handler, 配置变更后调用
plugin_manager.reload('custom_api') 重新导入即重新注册。Web 管理面板改用
register_page / register_route 实现。
"""

import asyncio
import base64
import json
import mimetypes
import os
import re
import time
import uuid

import requests
from aiohttp import web

from core.base.logger import PLUGIN, get_logger, report_error
from core.message._http import MessageType
from core.message._media_send import _set_msg_or_event_id
from core.plugin.decorators import handler, on_unload
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
    """配置变更后热重载本插件, 以便按新配置重新注册 @handler。"""
    try:
        from core.application import get_app

        app = get_app()
        if app and app.plugin_manager:
            await app.plugin_manager.reload('custom_api')
    except Exception as e:
        log.warning(f'热重载失败: {e}')


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
