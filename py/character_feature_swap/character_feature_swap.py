import json
import os
import folder_paths
from server import PromptServer
from comfy import model_management
import aiohttp
from aiohttp import web
import traceback
import asyncio
from ..utils.logger import get_logger

logger = get_logger(__name__)

def _parse_prompt_input(prompt_input):
    """
    尝试解析各种类型的输入:
    1. 如果是包含'prompt'键的JSON字符串，则提取'prompt'字段。
    2. 如果是元组或列表，则取第一个元素。
    3. 其他情况按原样返回。
    """
    # 检查是否为元组或列表，并至少有一个元素
    if isinstance(prompt_input, (list, tuple)) and prompt_input:
        # 递归处理第一个元素，以处理嵌套情况
        return _parse_prompt_input(prompt_input[0])
    
    # 检查是否为字符串
    if isinstance(prompt_input, str):
        try:
            # 尝试解析为JSON
            data = json.loads(prompt_input)
            if isinstance(data, dict) and 'prompt' in data:
                # 递归处理提取出的值
                return _parse_prompt_input(data['prompt'])
        except json.JSONDecodeError:
            # 如果不是有效的JSON，则按原样返回字符串
            return prompt_input
            
    # 对于所有其他情况（包括非字符串、非列表/元组），直接返回
    return prompt_input


def _diff_prompts(original_prompt, new_prompt):
    """
    对比原始prompt和新prompt，输出差异（新增/删除的tag）。
    Returns: 差异描述字符串
    """
    def split_tags(prompt):
        # 按逗号分割，去除首尾空格，过滤空项
        return [t.strip() for t in prompt.split(',') if t.strip()]

    orig_tags = set(split_tags(original_prompt))
    new_tags = set(split_tags(new_prompt))

    removed = orig_tags - new_tags
    added = new_tags - orig_tags

    lines = []
    if removed:
        lines.append("移除: " + ", ".join(sorted(removed)))
    if added:
        lines.append("新增: " + ", ".join(sorted(added)))
    if not removed and not added:
        lines.append("（无差异）")

    return "\n".join(lines)


def _parse_llm_output(raw_output, original_prompt=""):
    """
    解析LLM输出，将其分为两部分：
    1. new_prompt: 第一行（逗号分隔的danbooru tags）
    2. status: 替换列表 + 与原始prompt的差异对比（仅列出变化的tag）

    LLM输出格式：
    1girl, solo, short hair, ...

    **已替换列表**
    grey hair(灰发)→blue hair(蓝发)
    long hair(长发)→swept bangs(侧分刘海)
    ...

    Returns: (new_prompt, status)
    """
    if not raw_output or not raw_output.strip():
        return raw_output, ""

    text = raw_output.strip()

    import re

    # 匹配分隔标记（支持中英文，带或不带**）
    split_pattern = re.compile(
        r'\n\s*\*{0,2}(已替换列表|Replaced\s*List|替换列表)\*{0,2}\s*\n',
        re.IGNORECASE
    )

    match = split_pattern.search(text)

    if match:
        prompt_part = text[:match.start()].strip()
        replacement_part = text[match.end():].strip()

        prompt_lines = [line.strip() for line in prompt_part.split('\n') if line.strip()]
        new_prompt = prompt_lines[0] if prompt_lines else prompt_part

        status_lines = ["**已替换列表**"]
        if replacement_part:
            status_lines.append(replacement_part)
        else:
            status_lines.append("（无替换项）")

        # 差异对比：仅列出变化的tag
        if original_prompt and original_prompt.strip():
            status_lines.append("")
            status_lines.append("**Tag差异对比**")
            status_lines.append(_diff_prompts(original_prompt.strip(), new_prompt))

        status = "\n".join(status_lines)
        return new_prompt, status

    else:
        # 回退：检测→箭头行
        lines = text.split('\n')
        prompt_lines = []
        replacement_lines = []
        found_replacement = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if '→' in stripped or '->' in stripped:
                found_replacement = True
                replacement_lines.append(line)
            elif not found_replacement:
                prompt_lines.append(line)
            else:
                replacement_lines.append(line)

        if found_replacement and prompt_lines:
            new_prompt = prompt_lines[0].strip()

            status_lines = ["**已替换列表**"]
            if replacement_lines:
                status_lines.extend([l for l in replacement_lines if l.strip()])

            if original_prompt and original_prompt.strip():
                status_lines.append("")
                status_lines.append("**Tag差异对比**")
                status_lines.append(_diff_prompts(original_prompt.strip(), new_prompt))

            status = "\n".join(status_lines)
            return new_prompt, status

        # 完全无法解析
        non_empty_lines = [l.strip() for l in lines if l.strip()]
        new_prompt = non_empty_lines[0] if non_empty_lines else text

        status = "（未能解析替换列表）"
        if original_prompt and original_prompt.strip():
            status += "\n\n**Tag差异对比**\n" + _diff_prompts(original_prompt.strip(), new_prompt)

        return new_prompt, status


# 插件目录和设置文件路径
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LLM_SETTINGS_FILE = os.path.join(PLUGIN_DIR, "llm_settings.json")
PROMPT_CACHE_FILE = os.path.join(PLUGIN_DIR, "cache", "prompt_cache.json")

# 默认LLM设置
def load_llm_settings():
    """加载LLM设置，并处理从旧格式到新预设格式的迁移"""
    default_settings = {
        "api_channel": "openrouter",
        "api_url": "https://openrouter.ai/api/v1/chat/completions", # 将被弃用
        "api_key": "", # 将被弃用
        "model": "gryphe/mythomax-l2-13b", # 将被弃用
        "channel_models": {},
        "channels_config": { # 新增: 存储每个渠道的独立配置
            "openrouter": {"api_url": "https://openrouter.ai/api/v1", "api_key": ""},
            "gemini_api": {"api_url": "https://generativelanguage.googleapis.com/v1beta", "api_key": ""},
            "gemini_cli": {"api_url": "gemini_cli_mode", "api_key": ""},
            "deepseek": {"api_url": "https://api.deepseek.com/v1", "api_key": ""},
            "openai_compatible": {"api_url": "", "api_key": ""}
        },
        "timeout": 30,
        "custom_prompt": (
            "**原始提示:**\n{original_prompt}\n\n"
            "**新角色提示:**\n{character_prompt}\n\n"
            "**要替换的特征（指南）:**\n{target_features}\n\n"
            "**新提示:**"
        ),
        "language": "zh",
        "active_preset_name": "default",
        "presets": [
            {
                "name": "default",
                "features": [
                    "hair style", "hair color", "hair ornament",
                    "eye color", "unique body parts", "body shape", "ear shape"
                ]
            }
        ]
    }

    if not os.path.exists(LLM_SETTINGS_FILE):
        return default_settings

    try:
        with open(LLM_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            settings = json.load(f)

        migrated = False
        for key, value in default_settings.items():
            if key not in settings:
                migrated = True
                settings[key] = value
        
        if "channels_config" not in settings or not isinstance(settings["channels_config"], dict):
             settings["channels_config"] = default_settings["channels_config"]
             migrated = True

        old_api_url = settings.get("api_url")
        old_api_key = settings.get("api_key")
        old_channel = settings.get("api_channel", "openrouter")

        if old_api_url and old_api_key:
            if old_channel in settings["channels_config"] and not settings["channels_config"][old_channel].get("api_key"):
                logger.info(f"正在迁移渠道 '{old_channel}' 的旧 API Key...")
                settings["channels_config"][old_channel]["api_key"] = old_api_key
                if old_channel == "openai_compatible":
                    settings["channels_config"][old_channel]["api_url"] = old_api_url
                migrated = True

        if "presets" not in settings:
            migrated = True
            old_features = settings.get("target_features", default_settings["presets"][0]["features"])
            settings["presets"] = [{"name": "default", "features": old_features}]
            settings["active_preset_name"] = "default"
            if "target_features" in settings:
                del settings["target_features"]

        if migrated:
            if "api_url" in settings: del settings["api_url"]
            if "api_key" in settings: del settings["api_key"]
            if "model" in settings: del settings["model"]
            save_llm_settings(settings)

        return settings
    except Exception as e:
        logger.error(f"加载LLM设置失败: {e}")
        return default_settings

def save_llm_settings(settings):
    """保存LLM设置"""
    try:
        active_channel = settings.get("api_channel", "openrouter")
        
        if "channels_config" in settings and active_channel in settings["channels_config"]:
            channel_conf = settings["channels_config"][active_channel]
            settings["api_url"] = channel_conf.get("api_url", "")
            settings["api_key"] = channel_conf.get("api_key", "")

        if "channel_models" in settings and active_channel in settings["channel_models"]:
            settings["model"] = settings["channel_models"][active_channel]

        settings_to_save = settings.copy()
        if "api_url" in settings_to_save: del settings_to_save["api_url"]
        if "api_key" in settings_to_save: del settings_to_save["api_key"]
        if "model" in settings_to_save: del settings_to_save["model"]

        with open(LLM_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_to_save, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存LLM设置失败: {e}")
        return False

# 后端API路由
@PromptServer.instance.routes.get("/character_swap/llm_settings")
async def get_llm_settings(request):
    settings = load_llm_settings()
    return web.json_response(settings)

@PromptServer.instance.routes.post("/character_swap/llm_settings")
async def save_llm_settings_route(request):
    try:
        data = await request.json()
        if save_llm_settings(data):
            return web.json_response({"success": True})
        else:
            return web.json_response({"success": False, "error": "无法保存LLM设置"}, status=500)
    except Exception as e:
        logger.error(f"保存LLM设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

import sys
import shutil
import subprocess

def _get_gemini_executable_path():
    """
    Tries to find the full path to the gemini executable.
    """
    if sys.platform == "win32":
        try:
            npm_global_path = os.path.join(os.environ.get("APPDATA", ""), "npm")
            if os.path.exists(npm_global_path):
                logger.info(f"Adding npm global path to environment: {npm_global_path}")
                os.environ["PATH"] = npm_global_path + os.pathsep + os.environ["PATH"]
        except Exception as e:
            logger.error(f"Failed to add npm global path to PATH: {e}")

    gemini_executable = shutil.which("gemini")

    if gemini_executable:
        logger.info(f"Found Gemini CLI executable at: {gemini_executable}")
        return gemini_executable
    else:
        logger.error("Could not find 'gemini' executable.")
        return "gemini"

@PromptServer.instance.routes.post("/character_swap/llm_models")
async def get_llm_models(request):
    """根据提供的API凭据获取LLM模型列表"""
    try:
        data = await request.json()
        api_channel = data.get("api_channel")
        if not api_channel:
            return web.json_response({"error": "未提供渠道(api_channel)"}, status=400)

        settings = load_llm_settings()
        channels_config = settings.get("channels_config", {})
        channel_conf = channels_config.get(api_channel, {})
        
        api_url = channel_conf.get("api_url", "").strip()
        api_key = channel_conf.get("api_key", "").strip()
        timeout = settings.get("timeout", 15)

        logger.info(f"[get_llm_models] Channel: '{api_channel}', URL: '{api_url}'")

        if api_channel == "gemini_cli":
            return web.json_response(sorted([
                "gemini-1.5-flash-002",
                "gemini-1.5-flash-8b-exp-0827",
                "gemini-1.5-flash-exp-0827",
                "gemini-1.5-flash-latest",
                "gemini-1.5-pro-002",
                "gemini-1.5-pro-exp-0827",
                "gemini-1.5-pro-latest",
                "gemini-2.0-flash-001",
                "gemini-2.0-flash-exp",
                "gemini-2.0-flash-thinking-exp-01-21",
                "gemini-2.0-flash-thinking-exp-1219",
                "gemini-2.5-flash",
                "gemini-2.5-pro",
                "gemini-exp-1206",
                "gemini-pro",
            ]))

        if not api_url:
            return web.json_response({"error": "当前渠道的 API URL 为空"}, status=400)

        async with aiohttp.ClientSession() as session:
            if api_channel == 'gemini_api':
                if not api_key:
                    return web.json_response({"error": "Gemini API Key为空"}, status=400)
                
                models_url = f"{api_url.rstrip('/')}/models?key={api_key}"
                async with session.get(models_url, timeout=timeout, ssl=False) as response:
                    response.raise_for_status()
                    models_data = (await response.json()).get("models", [])
                    model_ids = sorted([
                        model["name"].split('/')[-1] for model in models_data
                        if "generateContent" in model.get("supportedGenerationMethods", [])
                    ])
                    return web.json_response(model_ids)

            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            models_url = f"{api_url.rstrip('/')}/models"
            logger.info(f"[get_llm_models] Attempting to get models from: {models_url}")
            
            async with session.get(models_url, headers=headers, timeout=timeout, ssl=False) as response:
                response.raise_for_status()
                models_data = (await response.json()).get("data", [])
                model_ids = sorted([model["id"] for model in models_data])
                return web.json_response(model_ids)
            
    except aiohttp.ClientResponseError as e:
        error_message = f"HTTP错误: {e.status} - {e.message}"
        logger.error(f"获取LLM模型列表失败: {error_message}")
        fallback_models = {
            "openrouter": ["gryphe/mythomax-l2-13b", "google/gemini-flash-1.5", "anthropic/claude-3-haiku"],
            "gemini_api": [
                "gemini-1.5-flash-latest", "gemini-1.5-pro-latest", "gemini-pro",
                "gemini-2.5-flash", "gemini-2.5-pro"
            ],
            "deepseek": ["deepseek-chat", "deepseek-coder"],
            "openai_compatible": ["default-model-1", "default-model-2"]
        }
        models = fallback_models.get(api_channel, [])
        logger.info(f"API call failed, returning fallback models for channel '{api_channel}': {models}")
        return web.json_response(models)
    except asyncio.TimeoutError:
        logger.error(f"获取LLM模型列表超时")
        return web.json_response({"error": f"请求超时: {timeout}s"}, status=500)
    except Exception as e:
        logger.error(f"处理模型列表时出错: {e}")
        return web.json_response({"error": f"未知错误: {e}"}, status=500)

@PromptServer.instance.routes.post("/character_swap/debug_prompt")
async def debug_llm_prompt(request):
    """构建并返回将发送给LLM的最终提示"""
    try:
        data = await request.json()
        original_prompt = data.get("original_prompt", "")
        character_prompt = data.get("character_prompt", "")
        target_features = data.get("target_features", [])

        logger.info(f"[Debug Prompt] Received data: original_prompt='{original_prompt}', character_prompt='{character_prompt}'")

        settings = load_llm_settings()
        custom_prompt_template = settings.get("custom_prompt", "")

        character_prompt_text = character_prompt or "[... features from character prompt ...]"
        original_prompt_text = original_prompt or "[... features from original prompt ...]"
        target_features_text = ", ".join(target_features) or "[... no features selected ...]"

        final_prompt = custom_prompt_template.format(
            original_prompt=original_prompt_text,
            character_prompt=character_prompt_text,
            target_features=target_features_text
        )

        logger.info(f"[Debug Prompt] Final prompt being sent to frontend: {final_prompt}")

        return web.json_response({"final_prompt": final_prompt})

    except Exception as e:
        logger.error(f"构建调试提示时出错: {e}")
        return web.json_response({"error": str(e)}, status=500)

# API to get cached prompts
@PromptServer.instance.routes.get("/character_swap/cached_prompts")
async def get_cached_prompts(request):
    if not os.path.exists(PROMPT_CACHE_FILE):
        return web.json_response({"original_prompt": "", "character_prompt": ""})
    try:
        with open(PROMPT_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return web.json_response(data)
    except Exception as e:
        logger.error(f"读取提示词缓存失败: {e}")
        return web.json_response({"error": str(e)}, status=500)

@PromptServer.instance.routes.post("/character_swap/test_llm_connection")
async def test_llm_connection(request):
    """测试与LLM API的连接和认证"""
    try:
        data = await request.json()
        api_channel = data.get("api_channel")
        api_url = data.get("api_url", "").strip()
        api_key = data.get("api_key", "").strip()
        timeout = data.get("timeout", 15)

        if not api_channel:
            return web.json_response({"success": False, "error": "未提供渠道(api_channel)"}, status=400)

        if api_channel == 'gemini_cli':
            gemini_executable = _get_gemini_executable_path()
            if gemini_executable and shutil.which(gemini_executable):
                 return web.json_response({"success": True, "message": "Gemini CLI 可访问。"})
            else:
                 return web.json_response({"success": False, "error": "找不到 Gemini CLI。请全局安装并确保在PATH中。"}, status=400)

        if not api_url or not api_key:
            return web.json_response({"success": False, "error": "当前渠道的 API URL 或 API Key 为空"}, status=400)

        async with aiohttp.ClientSession() as session:
            if api_channel == 'gemini_api':
                test_url = f"{api_url.rstrip('/')}/models?key={api_key}"
                async with session.get(test_url, timeout=timeout, ssl=False) as response:
                    response.raise_for_status()
                    if "models" in await response.json():
                        return web.json_response({"success": True, "message": "成功连接到 Gemini API。"})
                    else:
                        raise Exception("Gemini API 响应格式不正确。")

            test_url = f"{api_url.rstrip('/')}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            
            async with session.get(test_url, headers=headers, timeout=timeout, ssl=False) as response:
                response.raise_for_status()
                if "data" in await response.json():
                    return web.json_response({"success": True, "message": "成功连接到 API。"})
                else:
                    raise Exception("API 响应格式不正确。")

    except aiohttp.ClientResponseError as e:
        error_message = f"HTTP错误: {e.status} - {e.message}"
        logger.error(f"LLM连接测试失败: {error_message}")
        return web.json_response({"success": False, "error": error_message}, status=400)
    except asyncio.TimeoutError:
        logger.error(f"LLM连接测试超时")
        return web.json_response({"success": False, "error": f"请求超时: {timeout}s"}, status=500)
    except Exception as e:
        logger.error(f"LLM连接测试时发生未知错误: {e}")
        return web.json_response({"success": False, "error": f"未知错误: {e}"}, status=500)

@PromptServer.instance.routes.post("/character_swap/test_llm_response")
async def test_llm_response(request):
    """测试向指定模型发送消息并获得回复"""
    try:
        data = await request.json()
        api_channel = data.get("api_channel")
        api_url = data.get("api_url", "").strip()
        api_key = data.get("api_key", "").strip()
        model = data.get("model")
        timeout = data.get("timeout", 30)
        
        if not api_channel:
            return web.json_response({"success": False, "error": "未提供渠道(api_channel)"}, status=400)

        if not model:
            return web.json_response({"success": False, "error": "当前渠道未选择模型"}, status=400)

        if api_channel == "gemini_cli":
            try:
                gemini_executable = _get_gemini_executable_path()
                command = [gemini_executable, "-m", model]
                
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=b"Hello!"),
                    timeout=timeout
                )
                
                if process.returncode != 0:
                    raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
                    
                reply = stdout.decode('utf-8').strip()
                if not reply:
                    raise Exception("Gemini CLI returned an empty response.")
                
                return web.json_response({"success": True, "message": f"模型回复: '{reply}'"})

            except FileNotFoundError:
                return web.json_response({"success": False, "error": "找不到 Gemini CLI。"}, status=500)
            except asyncio.TimeoutError:
                return web.json_response({"success": False, "error": f"Gemini CLI 命令超时 ({timeout}s)。"}, status=500)
            except subprocess.CalledProcessError as e:
                error_output = e.stderr.decode('utf-8').strip()
                return web.json_response({"success": False, "error": f"Gemini CLI 错误: {error_output}"}, status=500)
            except Exception as e:
                return web.json_response({"success": False, "error": f"未知的 Gemini CLI 错误: {e}"}, status=500)

        if not api_url or not api_key:
            return web.json_response({"success": False, "error": "当前渠道的 API URL 或 API Key 为空"}, status=400)

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        async with aiohttp.ClientSession() as session:
            if api_channel == 'gemini_api':
                payload = { "contents": [{ "parts": [{ "text": "Hello!" }] }] }
                api_endpoint = f"{api_url.rstrip('/')}/models/{model}:generateContent?key={api_key}"
                headers = {"Content-Type": "application/json"}
                async with session.post(api_endpoint, headers=headers, json=payload, timeout=timeout, ssl=False) as response:
                    response.raise_for_status()
                    result = await response.json()
                    reply = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '').strip()
            else:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Hello!"}],
                    "max_tokens": 15
                }
                api_endpoint = f"{api_url.rstrip('/')}/chat/completions"
                async with session.post(api_endpoint, headers=headers, json=payload, timeout=timeout, ssl=False) as response:
                    response.raise_for_status()
                    result = await response.json()
                    reply = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()

            if not reply:
                 raise Exception("模型返回了空回复。")

            return web.json_response({"success": True, "message": f"模型回复: '{reply}'"})

    except aiohttp.ClientResponseError as e:
        error_message = f"HTTP错误: {e.status} - {e.message}"
        logger.error(f"LLM响应测试失败: {error_message}")
        return web.json_response({"success": False, "error": error_message}, status=400)
    except asyncio.TimeoutError:
        logger.error(f"LLM响应测试超时")
        return web.json_response({"success": False, "error": f"请求超时: {timeout}s"}, status=500)
    except Exception as e:
        logger.error(f"LLM响应测试时发生未知错误: {e}")
        return web.json_response({"success": False, "error": f"未知错误: {e}"}, status=500)

# API to get all tags
@PromptServer.instance.routes.get("/character_swap/get_all_tags")
async def get_all_tags(request):
    """提供所有可用的标签给前端，优先使用JSON，失败则回退到CSV"""
    zh_cn_dir = os.path.join(PLUGIN_DIR, "..", "danbooru_gallery", "zh_cn")
    json_file = os.path.join(zh_cn_dir, "all_tags_cn.json")
    csv_file = os.path.join(zh_cn_dir, "danbooru.csv")
    
    tags_data = {}

    if os.path.exists(json_file):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                tags_data = json.load(f)
            if isinstance(tags_data, dict) and tags_data:
                return web.json_response(tags_data)
        except Exception as e:
            logger.warning(f"加载 all_tags_cn.json 失败: {e}。尝试回退到 CSV。")
            tags_data = {}

    if not tags_data and os.path.exists(csv_file):
        try:
            import csv
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) >= 2:
                        tags_data[row[0]] = row[1]
            if tags_data:
                return web.json_response(tags_data)
        except Exception as e:
            logger.error(f"加载 danbooru.csv 也失败了: {e}")

    if not tags_data:
        return web.json_response({"error": "Tag files not found or are invalid."}, status=404)
    
    return web.json_response({"error": "An unknown error occurred while loading tags."}, status=500)

class CharacterFeatureSwapNode:
    """
    一个使用LLM API替换提示词中人物特征的节点
    输出:
      - new_prompt: 经过LLM替换后的提示词（仅tags行）
      - status: 替换列表 + 与原始prompt的对比信息
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "original_prompt": ("STRING", {"forceInput": True}),
                "character_prompt": ("STRING", {"forceInput": True}),
                "target_features": ("STRING", {"default": "hair style, hair color, hair ornament, eye color, unique body parts, body shape, ear shape", "multiline": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("new_prompt", "status", "raw_output")
    FUNCTION = "execute"
    CATEGORY = "danbooru"

    async def execute(self, original_prompt, character_prompt, target_features):
        """
        异步执行角色特征交换，支持ComfyUI中断机制
        输出: (new_prompt, status, raw_output)
          - new_prompt: 清洗后的tags行
          - status: 替换列表 + tag差异对比（仅列出变化的tag）
          - raw_output: LLM原始完整输出（用于调试）
        """
        model_management.throw_exception_if_processing_interrupted()
            
        original_prompt = _parse_prompt_input(original_prompt)
        character_prompt = _parse_prompt_input(character_prompt)

        model_management.throw_exception_if_processing_interrupted()

        settings = load_llm_settings()
        api_channel = settings.get("api_channel", "openrouter")
        channels_config = settings.get("channels_config", {})
        channel_conf = channels_config.get(api_channel, {})
        
        api_url = channel_conf.get("api_url", "").strip()
        api_key = channel_conf.get("api_key", "").strip()
        model = settings.get("channel_models", {}).get(api_channel)
        
        custom_prompt_template = settings.get("custom_prompt")
        timeout = settings.get("timeout", 30)

        active_preset_name = settings.get("active_preset_name", "default")
        active_preset = next((p for p in settings.get("presets", []) if p["name"] == active_preset_name), None)
        
        final_target_features = ", ".join(active_preset["features"]) if active_preset else target_features

        model_management.throw_exception_if_processing_interrupted()
            
        import re
        required_placeholders = {"original_prompt", "character_prompt", "target_features"}
        
        all_found_placeholders = re.findall(r"\{(.+?)\}", custom_prompt_template, re.DOTALL)
        cleaned_placeholders = set(ph.replace('\n', '').replace('\r', '') for ph in all_found_placeholders)

        missing = required_placeholders - cleaned_placeholders
        if missing:
            error_msg = f"错误: 自定义提示词模板缺少占位符: {', '.join(missing)}。请在设置中修复。"
            logger.error(error_msg)
            return (error_msg, "", "")

        malformed_errors = []
        for ph in all_found_placeholders:
            if '\n' in ph or '\r' in ph:
                display_ph = ph.replace('\n', '\\n').replace('\r', '\\r')
                correct_ph = ph.replace('\n', '').replace('\r', '')
                error = f"错误格式 '{{{display_ph}}}' -> 正确应为 '{{{correct_ph}}}'"
                malformed_errors.append(error)
        
        if malformed_errors:
            error_msg = "错误: 模板中发现格式错误的占位符:\n" + "\n".join(malformed_errors) + "\n请在设置中修复。"
            logger.error(error_msg)
            return (error_msg, "", "")

        try:
            prompt_for_llm = custom_prompt_template.format(
                original_prompt=original_prompt,
                character_prompt=character_prompt,
                target_features=final_target_features
            )
        except KeyError as e:
            error_msg = f"错误: 格式化提示词失败，未知的占位符: {e}。请检查自定义提示词模板。"
            logger.error(error_msg)
            return (error_msg, "", "")

        model_management.throw_exception_if_processing_interrupted()

        # 缓存原始和角色提示词
        try:
            cache_dir = os.path.dirname(PROMPT_CACHE_FILE)
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir)
            with open(PROMPT_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({"original_prompt": original_prompt, "character_prompt": character_prompt}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"写入提示词缓存失败: {e}")

        # --- Gemini CLI 执行 (异步) ---
        if api_channel == "gemini_cli":
            model_management.throw_exception_if_processing_interrupted()
            
            if not model:
                logger.error("[Execute Gemini CLI] Error: Model not selected for Gemini CLI channel.")
                return ("错误: Gemini CLI 渠道未选择模型。", "", "")
            
            process = None
            try:
                gemini_executable = _get_gemini_executable_path()
                command = [gemini_executable, "-m", model]
                
                cli_env = os.environ.copy()
                cli_env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=cli_env
                )

                model_management.throw_exception_if_processing_interrupted()
                
                process.stdin.write(prompt_for_llm.encode('utf-8'))
                await process.stdin.drain()
                process.stdin.close()

                start_time = asyncio.get_event_loop().time()
                check_interval = 0.025
                
                while process.returncode is None:
                    try:
                        model_management.throw_exception_if_processing_interrupted()
                    except model_management.InterruptProcessingException:
                        logger.warning("[Execute Gemini CLI] Interruption detected. Terminating subprocess.")
                        try:
                            process.terminate()
                            try:
                                await asyncio.wait_for(process.wait(), timeout=1.0)
                            except asyncio.TimeoutError:
                                logger.warning("[Execute Gemini CLI] Force killing subprocess.")
                                process.kill()
                                await process.wait()
                        except (ProcessLookupError, AttributeError):
                            pass
                        raise
                    
                    current_time = asyncio.get_event_loop().time()
                    if (current_time - start_time) > timeout:
                         logger.error(f"[Execute Gemini CLI] Subprocess timed out. Terminating.")
                         try:
                             process.terminate()
                             try:
                                 await asyncio.wait_for(process.wait(), timeout=1.0)
                             except asyncio.TimeoutError:
                                 process.kill()
                                 await process.wait()
                         except (ProcessLookupError, AttributeError):
                             pass
                         return (f"错误: Gemini CLI 命令超时 ({timeout}s)。", "", "")

                    await asyncio.sleep(check_interval)

                model_management.throw_exception_if_processing_interrupted()

                stdout, stderr = await process.communicate()
                stdout_res = stdout.decode('utf-8', errors='ignore').strip()
                stderr_res = stderr.decode('utf-8', errors='ignore').strip()

                if process.returncode != 0:
                    logger.error(f"[Execute Gemini CLI] Subprocess failed. Stderr: {stderr_res}")
                    return (f"Gemini CLI 错误: {stderr_res}", "", stderr_res)

                raw_output = stdout_res.strip('"')
                # ↓↓↓ 解析LLM输出 ↓↓↓
                new_prompt, status = _parse_llm_output(raw_output, original_prompt)
                return (new_prompt, status, raw_output)

            except asyncio.CancelledError:
                logger.warning("[Execute Gemini CLI] Execution was cancelled.")
                if process:
                    try:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            process.kill()
                            await process.wait()
                    except (ProcessLookupError, AttributeError):
                        pass
                return ("错误: 执行被用户中断。", "", "")
            except Exception as e:
                logger.error(f"Gemini CLI 未知错误: {traceback.format_exc()}")
                if process:
                    try:
                        process.kill()
                        await process.wait()
                    except (ProcessLookupError, AttributeError):
                        pass
                return (f"Gemini CLI 未知错误: {e}", "", "")

        # --- HTTP API 执行 (异步) ---
        model_management.throw_exception_if_processing_interrupted()
            
        if not api_key:
            return (f"错误: 渠道 '{api_channel}' 的 API Key 未设置。", "", "")
        if not model:
            return (f"错误: 渠道 '{api_channel}' 的模型未选择。", "", "")

        model_management.throw_exception_if_processing_interrupted()

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        if api_channel == 'gemini_api':
            api_endpoint = f"{api_url.rstrip('/')}/models/{model}:generateContent?key={api_key}"
            headers = {"Content-Type": "application/json"}
            payload = {"contents": [{"parts": [{"text": prompt_for_llm}]}]}
        else:
            api_endpoint = f"{api_url.rstrip('/')}/chat/completions"
            payload = {"model": model, "messages": [{"role": "user", "content": prompt_for_llm}]}

        model_management.throw_exception_if_processing_interrupted()

        try:
            async with aiohttp.ClientSession() as session:
                async def make_http_request():
                    return await session.post(api_endpoint, headers=headers, json=payload, timeout=timeout, ssl=False)
                
                request_task = asyncio.create_task(make_http_request())
                
                check_interval = 0.05
                while not request_task.done():
                    try:
                        model_management.throw_exception_if_processing_interrupted()
                    except model_management.InterruptProcessingException:
                        logger.warning("[Execute HTTP] Interruption detected. Cancelling HTTP request.")
                        request_task.cancel()
                        try:
                            await request_task
                        except asyncio.CancelledError:
                            pass
                        raise
                    
                    try:
                        await asyncio.wait_for(asyncio.shield(request_task), timeout=check_interval)
                        break
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        raise model_management.InterruptProcessingException()

                response = request_task.result()
                response.raise_for_status()
                
                model_management.throw_exception_if_processing_interrupted()
                
                result = await response.json()

                model_management.throw_exception_if_processing_interrupted()

                if api_channel == 'gemini_api':
                    raw_output = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '').strip()
                else:
                    raw_output = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                
                raw_output = raw_output.strip('"')

                if not raw_output:
                    return ("错误: API 返回了空回复。", "", "")

                # ↓↓↓ 解析LLM输出为 new_prompt + status，保留 raw_output ↓↓↓
                new_prompt, status = _parse_llm_output(raw_output, original_prompt)
                return (new_prompt, status, raw_output)

        except asyncio.CancelledError:
            logger.warning("LLM API 调用被用户取消。")
            raise model_management.InterruptProcessingException()
        except aiohttp.ClientResponseError as e:
            error_details = ""
            try:
                error_details = await e.text()
            except:
                pass
            error_message = f"错误: API 请求失败 (HTTP {e.status})。详情: {error_details}"
            logger.error(error_message)
            return (error_message, "", "")
        except asyncio.TimeoutError:
            logger.error(f"调用LLM API超时")
            return (f"API Error: Request timed out after {timeout} seconds.", "", "")
        except Exception as e:
            logger.error(f"处理LLM响应失败: {traceback.format_exc()}")
            return (f"Processing Error: {e}", "", "")


# 节点映射
NODE_CLASS_MAPPINGS = {
    "CharacterFeatureSwapNode": CharacterFeatureSwapNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CharacterFeatureSwapNode": "角色特征交换 (Character Feature Swap)"
}
