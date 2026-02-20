import requests
import json
import folder_paths
from server import PromptServer
from aiohttp import web
import time
import torch
import io
import urllib.request
import urllib.parse
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import os
import csv
import re
from requests.auth import HTTPBasicAuth
import urllib3
from pathlib import Path
import sys
import threading
import concurrent.futures

# 导入日志器
from ..utils.logger import get_logger
logger = get_logger(__name__)

# 导入数据库管理器
try:
    from ..shared.db.db_manager import get_db_manager
except ImportError as e:
    logger.warning(f"[Autocomplete] 无法导入数据库管理器，将仅使用远程API模式: {e}")
    get_db_manager = None

# 禁用 SSL 警告（如果需要禁用证书验证）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Danbooru API文档链接 https://danbooru.donmai.us/wiki_pages/help:api
# Danbooru API的基础URL
BASE_URL = "https://danbooru.donmai.us"

# 获取插件目录路径
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")

# ================================
# 异步加载相关全局变量
# ================================
# 全局图像缓存：key=任务ID，value={create_time: 创建时间, total: 总图像数, images: [{loaded: 布尔值, tensor: 图像张量}]}
image_cache = {}
cache_lock = threading.Lock()  # 线程安全锁
# 线程池（控制并发加载数量，避免占用过多资源）
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# 缓存清理定时器（每10分钟清理30分钟前的过期任务）
def clean_expired_cache():
    while True:
        time.sleep(600)  # 10分钟检查一次
        with cache_lock:
            current_time = time.time()
            expired_tasks = [
                tid for tid, data in image_cache.items() 
                if "create_time" in data and current_time - data["create_time"] > 1800
            ]
            for tid in expired_tasks:
                del image_cache[tid]
                logger.info(f"清理过期缓存任务: {tid}")

# 启动缓存清理线程（守护线程，随程序退出）
clean_thread = threading.Thread(target=clean_expired_cache, daemon=True)
clean_thread.start()

# ================================
# 原始设置加载/保存函数（保持不变）
# ================================
def load_settings():
    """从本地文件加载所有设置"""
    default_settings = {
        "language": "zh",
        "blacklist": [],
        "filter_tags": [
            "watermark", "sample_watermark", "weibo_username", "weibo", "weibo_logo",
            "weibo_watermark", "censored", "mosaic_censoring", "artist_name", "twitter_username"
        ],
        "filter_enabled": True,
        "danbooru_username": "",
        "danbooru_api_key": "",
        "favorites": [],
        "debug_mode": False,
        "cache_enabled": True,
        "max_cache_age": 3600,
        "default_page_size": 20,
        "autocomplete_enabled": True,
        "tooltip_enabled": True,
        "autocomplete_max_results": 20,
        "selected_categories": ["copyright", "character", "general"]
    }
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for key, value in default_settings.items():
                    if key not in data:
                        data[key] = value
                return data
    except Exception as e:
        logger.error(f"加载设置失败: {e}")
    return default_settings

def load_autocomplete_config():
    """加载自动补全配置（用于数据库优先+API fallback机制）"""
    default_config = {
        "offline_mode": {
            "enabled": True,
            "fallback_to_remote": True,
            "remote_timeout_ms": 2000  # 2秒超时
        },
        "cache": {
            "use_database_query": True
        }
    }
    config_paths = [
        Path(PLUGIN_DIR) / "config.json",
        Path(PLUGIN_DIR).parent / "config.json",
    ]
    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if "offline_mode" in loaded:
                        default_config["offline_mode"].update(loaded["offline_mode"])
                    if "cache" in loaded:
                        default_config["cache"].update(loaded["cache"])
                    logger.info(f"[Autocomplete] 加载配置: {config_path}")
                    return default_config
            except Exception as e:
                logger.warning(f"[Autocomplete] 配置文件加载失败 {config_path}: {e}")
    logger.info("[Autocomplete] 使用默认配置")
    return default_config

def save_settings(settings):
    """保存所有设置到本地文件"""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存设置失败: {e}")
        return False

def load_user_auth():
    """从统一设置文件加载用户认证信息"""
    settings = load_settings()
    return settings.get("danbooru_username", ""), settings.get("danbooru_api_key", "")

def save_user_auth(username, api_key):
    """保存用户认证信息到统一设置文件"""
    settings = load_settings()
    settings["danbooru_username"] = username
    settings["danbooru_api_key"] = api_key
    return save_settings(settings)

def load_favorites():
    """从统一设置文件加载收藏列表"""
    settings = load_settings()
    return settings.get("favorites", [])

def save_favorites(favorites):
    """保存收藏列表到统一设置文件"""
    settings = load_settings()
    settings["favorites"] = favorites
    return save_settings(settings)

def load_language():
    """从统一设置文件加载语言设置"""
    settings = load_settings()
    return settings.get("language", "zh")

def save_language(language):
    """保存语言设置到统一设置文件"""
    settings = load_settings()
    settings["language"] = language
    return save_settings(settings)

def load_blacklist():
    """从统一设置文件加载黑名单"""
    settings = load_settings()
    return settings.get("blacklist", [])

def save_blacklist(blacklist_items):
    """保存黑名单到统一设置文件"""
    settings = load_settings()
    settings["blacklist"] = blacklist_items
    return save_settings(settings)

def load_filter_tags():
    """从统一设置文件加载提示词过滤设置"""
    settings = load_settings()
    return settings.get("filter_tags", []), settings.get("filter_enabled", True)

def save_filter_tags(filter_tags, enabled):
    """保存提示词过滤设置到统一设置文件"""
    settings = load_settings()
    settings["filter_tags"] = filter_tags
    settings["filter_enabled"] = enabled
    return save_settings(settings)

def load_ui_settings():
    """从统一设置文件加载UI设置"""
    settings = load_settings()
    return {
        "autocomplete_enabled": settings.get("autocomplete_enabled", True),
        "tooltip_enabled": settings.get("tooltip_enabled", True),
        "autocomplete_max_results": settings.get("autocomplete_max_results", 20),
        "selected_categories": settings.get("selected_categories", ["copyright", "character", "general"]),
        "multi_select_enabled": settings.get("multi_select_enabled", False)
    }

def save_ui_settings(ui_settings):
    """保存UI设置到统一设置文件"""
    settings = load_settings()
    settings["autocomplete_enabled"] = ui_settings.get("autocomplete_enabled", True)
    settings["tooltip_enabled"] = ui_settings.get("tooltip_enabled", True)
    settings["autocomplete_max_results"] = ui_settings.get("autocomplete_max_results", 20)
    settings["selected_categories"] = ui_settings.get("selected_categories", ["copyright", "character", "general"])
    settings["multi_select_enabled"] = ui_settings.get("multi_select_enabled", False)
    return save_settings(settings)

# ================================
# Tag翻译系统（保持不变）
# ================================
class TagTranslationSystem:
    """Tag翻译系统，负责加载、处理和查询汉化数据"""
    
    def __init__(self):
        self.en_to_cn = {}  # 英文->中文映射
        self.cn_to_en = {}  # 中文->英文映射
        self.cn_search_index = {}  # 中文搜索索引
        self.loaded = False
        self._translation_cache = {}  # 翻译缓存
        self._search_cache = {}  # 搜索缓存
        self.max_cache_size = 1000  # 最大缓存条目数
        
    def load_translation_data(self):
        """加载所有汉化数据文件"""
        if self.loaded:
            return True
            
        try:
            zh_cn_dir = os.path.join(PLUGIN_DIR, "zh_cn")
            
            # 加载JSON格式数据
            self._load_json_data(zh_cn_dir)
            # 加载CSV格式数据
            self._load_csv_data(zh_cn_dir)
            # 加载角色CSV数据
            self._load_character_csv_data(zh_cn_dir)
            
            # 构建下划线匹配映射
            self._build_underscore_variants()
            # 构建中文搜索索引
            self._build_chinese_search_index()
            
            self.loaded = True
            return True
            
        except Exception as e:
            logger.error(f"[翻译系统] 加载失败: {e}")
            return False
    
    def _load_json_data(self, zh_cn_dir):
        """加载JSON格式的翻译数据"""
        json_file = os.path.join(zh_cn_dir, "all_tags_cn.json")
        if os.path.exists(json_file):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for en_tag, cn_tag in data.items():
                        if en_tag and cn_tag:
                            self.en_to_cn[en_tag.strip()] = cn_tag.strip()
                            self.cn_to_en[cn_tag.strip()] = en_tag.strip()
            except Exception as e:
                logger.error(f"[翻译系统] JSON加载失败: {e}")
    
    def _load_csv_data(self, zh_cn_dir):
        """加载CSV格式的翻译数据"""
        csv_file = os.path.join(zh_cn_dir, "danbooru.csv")
        if os.path.exists(csv_file):
            try:
                with open(csv_file, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    count = 0
                    for row in reader:
                        if len(row) >= 2 and row[0] and row[1]:
                            en_tag = row[0].strip()
                            cn_tag = row[1].strip()
                            if en_tag not in self.en_to_cn:
                                self.en_to_cn[en_tag] = cn_tag
                            if cn_tag not in self.cn_to_en:
                                self.cn_to_en[cn_tag] = en_tag
                            count += 1
            except Exception as e:
                logger.error(f"[翻译系统] CSV加载失败: {e}")
    
    def _load_character_csv_data(self, zh_cn_dir):
        """加载角色CSV格式的翻译数据（格式：中文名称,英文tag）"""
        csv_file = os.path.join(zh_cn_dir, "wai_characters.csv")
        if os.path.exists(csv_file):
            try:
                with open(csv_file, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    count = 0
                    for row in reader:
                        if len(row) >= 2 and row[0] and row[1]:
                            cn_tag = row[0].strip()
                            en_tag = row[1].strip()
                            if en_tag not in self.en_to_cn:
                                self.en_to_cn[en_tag] = cn_tag
                            if cn_tag not in self.cn_to_en:
                                self.cn_to_en[cn_tag] = en_tag
                            count += 1
            except Exception as e:
                logger.error(f"[翻译系统] 角色CSV加载失败: {e}")
    
    def _build_underscore_variants(self):
        """构建下划线变体映射，处理有无下划线的匹配问题"""
        variants_to_add = {}
        
        for en_tag, cn_tag in list(self.en_to_cn.items()):
            if '_' in en_tag:
                no_underscore = en_tag.replace('_', '')
                if no_underscore not in self.en_to_cn:
                    variants_to_add[no_underscore] = cn_tag
            else:
                with_underscore = re.sub(r'(\d)([a-zA-Z])', r'\1_\2', en_tag)
                if with_underscore != en_tag and with_underscore not in self.en_to_cn:
                    variants_to_add[with_underscore] = cn_tag
        
        self.en_to_cn.update(variants_to_add)
    
    def _build_chinese_search_index(self):
        """构建中文搜索索引，支持部分匹配"""
        for cn_tag in self.cn_to_en.keys():
            for i, char in enumerate(cn_tag):
                if char not in self.cn_search_index:
                    self.cn_search_index[char] = set()
                self.cn_search_index[char].add(cn_tag)
                
                for length in [2, 3]:
                    if i + length <= len(cn_tag):
                        substring = cn_tag[i:i + length]
                        if substring not in self.cn_search_index:
                            self.cn_search_index[substring] = set()
                        self.cn_search_index[substring].add(cn_tag)
        
        for key in self.cn_search_index:
            self.cn_search_index[key] = list(self.cn_search_index[key])
            
    
    def translate_tag(self, en_tag):
        """翻译单个英文tag到中文"""
        if not self.loaded:
            self.load_translation_data()
        
        tag_key = en_tag.strip()
        if tag_key in self._translation_cache:
            return self._translation_cache[tag_key]
        
        translation = self.en_to_cn.get(tag_key)
        if len(self._translation_cache) < self.max_cache_size:
            self._translation_cache[tag_key] = translation
        
        return translation
    
    def translate_tags_batch(self, en_tags):
        """批量翻译英文tags"""
        if not self.loaded:
            self.load_translation_data()
        
        result = {}
        for tag in en_tags:
            translation = self.en_to_cn.get(tag.strip())
            if translation:
                result[tag] = translation
        return result
    
    def search_chinese_tags(self, query, limit=10):
        """搜索中文tag，返回匹配的中文tag及对应英文tag，支持模糊搜索"""
        if not self.loaded:
            self.load_translation_data()
        
        query = query.strip()
        if not query:
            return []
        
        cache_key = f"{query}:{limit}"
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]
        
        matches = {}
        
        # 精确匹配（权重10）
        if query in self.cn_to_en:
            matches[query] = 10
        
        # 前缀匹配（权重8）
        for cn_tag in self.cn_to_en.keys():
            if cn_tag.startswith(query) and cn_tag not in matches:
                matches[cn_tag] = 8
        
        # 索引匹配（权重6）
        if query in self.cn_search_index:
            for cn_tag in self.cn_search_index[query]:
                if cn_tag not in matches:
                    matches[cn_tag] = 6
        
        # 包含匹配（权重4）
        for cn_tag in self.cn_to_en.keys():
            if query in cn_tag and cn_tag not in matches:
                matches[cn_tag] = 4
        
        # 模糊匹配（权重2）
        if len(query) >= 2:
            query_chars = set(query)
            for cn_tag in self.cn_to_en.keys():
                if cn_tag not in matches:
                    tag_chars = set(cn_tag)
                    if len(query_chars & tag_chars) / len(query_chars) >= 0.5:
                        matches[cn_tag] = 2
        
        # 部分字符匹配（权重1）
        for char in query:
            if char in self.cn_search_index:
                for cn_tag in self.cn_search_index[char]:
                    if cn_tag not in matches:
                        matches[cn_tag] = 1
        
        sorted_matches = sorted(matches.items(), key=lambda x: (-x[1], len(x[0])))
        results = []
        for cn_tag, weight in sorted_matches[:limit]:
            en_tag = self.cn_to_en.get(cn_tag)
            if en_tag:
                results.append({
                    'chinese': cn_tag,
                    'english': en_tag,
                    'weight': weight
                })
        
        if len(self._search_cache) < self.max_cache_size:
            self._search_cache[cache_key] = results
        
        return results

# 全局翻译系统实例
translation_system = TagTranslationSystem()

# 预加载翻译数据
def preload_translation_data():
    """预加载翻译数据，在服务器启动时调用"""
    try:
        success = translation_system.load_translation_data()
        if not success:
            logger.warning("[翻译系统] 预加载失败")
    except Exception as e:
        logger.error(f"[翻译系统] 预加载异常: {e}")

# 在模块加载时预加载翻译数据
preload_translation_data()

# ================================
# 网络/认证相关函数（保持不变）
# ================================
def check_network_connection():
    """检测与Danbooru的网络连接状态"""
    try:
        test_url = f"{BASE_URL}/posts.json?limit=1"
        response = requests.get(test_url, timeout=10)
        return response.status_code == 200, False
    except requests.exceptions.Timeout:
        logger.error("网络连接超时")
        return False, True
    except requests.exceptions.RequestException as e:
        logger.error(f"网络连接失败: {e}")
        return False, True
    except Exception as e:
        logger.error(f"网络检测发生未知错误: {e}")
        return False, True

def verify_danbooru_auth(username, api_key):
    """验证Danbooru用户认证"""
    if not username or not api_key:
        return False, False
    try:
        test_url = f"{BASE_URL}/profile.json"
        response = requests.get(test_url, auth=HTTPBasicAuth(username, api_key), timeout=15)
        is_valid = response.status_code == 200
        return is_valid, False
    except Exception as e:
        logger.error(f"验证用户认证失败: {e}")
        return False, True

def get_user_favorites(username, api_key):
    """获取用户的收藏列表"""
    try:
        favorites_url = f"{BASE_URL}/favorites.json"
        response = requests.get(favorites_url, auth=HTTPBasicAuth(username, api_key), timeout=15)
        if response.status_code == 200:
            return response.json()
        return []
    except Exception as e:
        logger.error(f"获取用户收藏列表失败: {e}")
        return []

# ================================
# 路由接口（保持不变）
# ================================
@PromptServer.instance.routes.post("/danbooru_gallery/favorites/add")
async def add_favorite(request):
    """添加收藏"""
    try:
        data = await request.json()
        post_id = data.get("post_id")
        if not post_id:
            return web.json_response({"success": False, "error": "缺少post_id"})
        username, api_key = load_user_auth()
        if not username or not api_key:
            return web.json_response({"success": False, "error": "请先在设置中配置用户名和API Key"})
        # 验证认证
        is_valid, is_network_error = verify_danbooru_auth(username, api_key)
        if is_network_error:
            return web.json_response({"success": False, "error": "网络错误，无法连接到Danbooru服务器"})
        if not is_valid:
            return web.json_response({"success": False, "error": "认证无效，请检查用户名和API Key"})
        try:
            favorite_url = f"{BASE_URL}/favorites.json"
            response = requests.post(
                favorite_url,
                auth=HTTPBasicAuth(username, api_key),
                data={"post_id": post_id},
                timeout=15
            )
            if response.status_code in [200, 201]:
                favorites = load_favorites()
                if str(post_id) not in favorites:
                    favorites.append(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "收藏成功"})
            
            try:
                error_data = response.json()
                reason = error_data.get("reason", "未知")
                message = error_data.get("message", "没有提供具体信息")
            except (json.JSONDecodeError, ValueError):
                error_data = {}
                reason = "无法解析响应"
                message = response.text
            if response.status_code == 422 and "You have already favorited this post" in message:
                favorites = load_favorites()
                if str(post_id) not in favorites:
                    favorites.append(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "已收藏，无需重复操作"})
                
            error_map = {
                401: "认证失败，请检查用户名和API Key",
                403: "权限不足，可能需要Gold账户或更高权限",
                404: "图片不存在",
                429: "请求过于频繁，请稍后重试 (Rate Limited)",
            }
            
            error_message = error_map.get(response.status_code, f"收藏失败，状态码: {response.status_code}, 原因: {message}")
            logger.error(error_message)
            return web.json_response({"success": False, "error": error_message})
        except requests.exceptions.Timeout:
            logger.error("添加收藏时网络请求超时")
            return web.json_response({"success": False, "error": "网络请求超时"})
        except requests.exceptions.RequestException as e:
            logger.error(f"添加收藏时网络请求失败: {e}")
            return web.json_response({"success": False, "error": f"网络请求失败: {e}"})
        except Exception as e:
            import traceback
            logger.error(f"添加收藏时发生严重错误: {e}")
            logger.error(traceback.format_exc())
            return web.json_response({"success": False, "error": f"服务器内部错误: {e}"}, status=500)
    except Exception as e:
        logger.error(f"添加收藏接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/favorites/remove")
async def remove_favorite(request):
    """移除收藏"""
    try:
        data = await request.json()
        post_id = data.get("post_id")
        if not post_id:
            return web.json_response({"success": False, "error": "缺少post_id"})
        
        username, api_key = load_user_auth()
        if not username or not api_key:
            return web.json_response({"success": False, "error": "请先在设置中配置用户名和API Key"})
        # 验证认证
        is_valid, is_network_error = verify_danbooru_auth(username, api_key)
        if is_network_error:
            return web.json_response({"success": False, "error": "网络错误，无法连接到Danbooru服务器"})
        if not is_valid:
            return web.json_response({"success": False, "error": "认证无效，请检查用户名和API Key"})
        
        try:
            # 直接使用帖子ID删除收藏
            delete_url = f"{BASE_URL}/favorites/{post_id}.json"
            delete_response = requests.delete(delete_url, auth=HTTPBasicAuth(username, api_key), timeout=15)
            if delete_response.status_code in [200, 204]:
                favorites = load_favorites()
                if str(post_id) in favorites:
                    favorites.remove(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "取消收藏成功"})
            elif delete_response.status_code == 404:
                # 如果收藏不存在，视为已删除
                favorites = load_favorites()
                if str(post_id) in favorites:
                    favorites.remove(str(post_id))
                    save_favorites(favorites)
                return web.json_response({"success": True, "message": "该图片未在云端收藏，本地已同步"})
            # 如果有收藏记录但删除失败，解析错误
            try:
                error_data = delete_response.json()
                reason = error_data.get("reason", "未知")
                message = error_data.get("message", "没有提供具体信息")
            except (json.JSONDecodeError, ValueError):
                error_data = {}
                reason = "无法解析响应"
                message = delete_response.text
            error_map = {
                401: "认证失败，请检查用户名和API Key",
                403: "权限不足，可能需要Gold账户",
                404: "收藏记录不存在",
                429: "请求过于频繁，请稍后重试 (Rate Limited)",
            }
            error_message = error_map.get(delete_response.status_code, f"取消收藏失败，状态码: {delete_response.status_code}, 原因: {message}")
            logger.error(error_message)
            return web.json_response({"success": False, "error": error_message})
        except requests.exceptions.Timeout:
            logger.error("移除收藏时网络请求超时")
            return web.json_response({"success": False, "error": "网络请求超时"})
        except requests.exceptions.RequestException as e:
            logger.error(f"移除收藏时网络请求失败: {e}")
            return web.json_response({"success": False, "error": f"网络请求失败: {e}"})
        except Exception as e:
            import traceback
            logger.error(f"移除收藏时发生严重错误: {e}")
            logger.error(traceback.format_exc())
            return web.json_response({"success": False, "error": f"服务器内部错误: {e}"}, status=500)
    except Exception as e:
        logger.error(f"移除收藏接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/user_auth")
async def get_user_auth_route(request):
    """获取用户认证信息"""
    try:
        username, api_key = load_user_auth()
        has_auth = bool(username and api_key)
        return web.json_response({"success": True, "username": username, "api_key": api_key, "has_auth": has_auth})
    except Exception as e:
        logger.error(f"获取用户认证接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/favorites")
async def get_favorites_route(request):
    """获取收藏列表"""
    try:
        favorites = load_favorites()
        return web.json_response({"success": True, "favorites": favorites})
    except Exception as e:
        logger.error(f"获取收藏列表接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/user_auth")
async def save_user_auth_route(request):
    """保存用户认证信息"""
    try:
        data = await request.json()
        username = data.get("username", "")
        api_key = data.get("api_key", "")
        if save_user_auth(username, api_key):
            return web.json_response({"success": True})
        else:
            return web.json_response({"success": False, "error": "无法保存用户认证信息"}, status=500)
    except Exception as e:
        logger.error(f"保存用户认证接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/check_network")
async def check_network(request):
    """检测网络连接状态"""
    try:
        is_connected, is_network_error = check_network_connection()
        return web.json_response({"success": True, "connected": is_connected, "network_error": is_network_error})
    except Exception as e:
        logger.error(f"网络检测接口错误: {e}")
        return web.json_response({"success": False, "error": "网络检测失败", "network_error": True}, status=500)

@PromptServer.instance.routes.post("/danbooru_gallery/verify_auth")
async def verify_auth(request):
    """验证用户认证"""
    try:
        data = await request.json()
        username = data.get("username", "")
        api_key = data.get("api_key", "")
        if not username or not api_key:
            return web.json_response({"success": False, "error": "缺少用户名或API Key"})
        is_valid, is_network_error = verify_danbooru_auth(username, api_key)
        return web.json_response({"success": True, "valid": is_valid, "network_error": is_network_error})
    except Exception as e:
        logger.error(f"验证认证接口错误: {e}")
        return web.json_response({"success": False, "error": "网络错误", "network_error": True}, status=500)

@PromptServer.instance.routes.get("/danbooru_gallery/posts")
async def get_posts_for_front(request):
    query = request.query
    tags = query.get("search[tags]", "")
    page = query.get("page", "1")
    limit = query.get("limit", "100")
    rating = query.get("search[rating]", "")
    posts_json_str, = DanbooruGalleryNode.get_posts_internal(tags=tags, limit=int(limit), page=int(page), rating=rating)
    
    try:
        posts_list = json.loads(posts_json_str)
    except json.JSONDecodeError:
        posts_list = []
    return web.json_response(posts_list, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

@PromptServer.instance.routes.get("/danbooru_gallery/autocomplete")
async def get_autocomplete(request):
    """三层查询机制：数据库 → API → 空结果"""
    try:
        query = request.query.get("query", "")
        limit = int(request.query.get("limit", "20"))
        if not query:
            return web.json_response([])
        # 加载配置
        config = load_autocomplete_config()
        # ✅ 第1层：查询本地SQLite数据库
        if get_db_manager and config['cache'].get('use_database_query', True):
            try:
                db = get_db_manager()
                db_results = await db.search_tags_by_prefix(query, limit)
                if db_results:
                    # 数据库有结果，转换格式并返回
                    formatted_results = [
                        {
                            'name': tag['tag'],
                            'category': tag['category'],
                            'post_count': tag['post_count'],
                            'translation': tag.get('translation_cn'),
                            'aliases': tag.get('aliases', [])
                        }
                        for tag in db_results
                    ]
                    logger.debug(f"[Autocomplete] 数据库查询成功: '{query}' -> {len(formatted_results)}条结果")
                    return web.json_response(formatted_results)
                else:
                    logger.debug(f"[Autocomplete] 数据库无结果: '{query}'")
            except Exception as e:
                logger.warning(f"[Autocomplete] 数据库查询失败: {e}，尝试API fallback")
        # ✅ 第2层：Fallback到Danbooru API
        if config['offline_mode'].get('fallback_to_remote', True):
            try:
                timeout = config['offline_mode'].get('remote_timeout_ms', 2000) / 1000.0
                tags_url = f"{BASE_URL}/tags.json"
                params = {
                    "search[name_or_alias_matches]": f"{query}*",
                    "search[order]": "count",
                    "limit": limit
                }
                username, api_key = load_user_auth()
                auth = HTTPBasicAuth(username, api_key) if username and api_key else None
                logger.debug(f"[Autocomplete] 调用远程API: '{query}' (超时: {timeout}s)")
                response = requests.get(tags_url, params=params, auth=auth, timeout=timeout)
                response.raise_for_status()
                result = response.json()
                # 排序确保按热度排列
                if isinstance(result, list):
                    result.sort(key=lambda x: x.get('post_count', 0), reverse=True)
                    logger.info(f"[Autocomplete] API查询成功: '{query}' -> {len(result)}条结果")
                return web.json_response(result)
            except requests.Timeout:
                logger.warning(f"[Autocomplete] 远程API超时 (>{timeout}s): '{query}'")
            except requests.exceptions.RequestException as e:
                logger.warning(f"[Autocomplete] 远程API失败: {e}")
            except Exception as e:
                logger.error(f"[Autocomplete] API调用错误: {e}")
        # ✅ 第3层：返回空结果
        logger.debug(f"[Autocomplete] 所有查询方式均无结果: '{query}'")
        return web.json_response([])
    except Exception as e:
        logger.error(f"[Autocomplete] 处理请求时发生错误: {e}")
        return web.json_response([])

@PromptServer.instance.routes.get("/danbooru_gallery/blacklist")
async def get_blacklist(request):
    blacklist = load_blacklist()
    return web.json_response({"blacklist": blacklist})

@PromptServer.instance.routes.post("/danbooru_gallery/blacklist")
async def save_blacklist_route(request):
    try:
        data = await request.json()
        blacklist_items = data.get("blacklist", [])
        success = save_blacklist(blacklist_items)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存黑名单接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/language")
async def get_language(request):
    language = load_language()
    return web.json_response({"language": language})

@PromptServer.instance.routes.post("/danbooru_gallery/language")
async def save_language_route(request):
    try:
        data = await request.json()
        language = data.get("language", "zh")
        success = save_language(language)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存语言设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/filter_tags")
async def get_filter_tags(request):
    filter_tags, filter_enabled = load_filter_tags()
    return web.json_response({"filter_tags": filter_tags, "filter_enabled": filter_enabled})

@PromptServer.instance.routes.post("/danbooru_gallery/filter_tags")
async def save_filter_tags_route(request):
    try:
        data = await request.json()
        filter_tags = data.get("filter_tags", [])
        filter_enabled = data.get("filter_enabled", False)
        success = save_filter_tags(filter_tags, filter_enabled)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存提示词过滤设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/ui_settings")
async def get_ui_settings(request):
    try:
        ui_settings = load_ui_settings()
        return web.json_response({
            "success": True,
            "settings": ui_settings
        })
    except Exception as e:
        logger.error(f"[UI_SETTINGS] 获取UI设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/ui_settings")
async def save_ui_settings_route(request):
    try:
        data = await request.json()
        ui_settings = {
            "autocomplete_enabled": data.get("autocomplete_enabled", True),
            "tooltip_enabled": data.get("tooltip_enabled", True),
            "autocomplete_max_results": data.get("autocomplete_max_results", 20),
            "selected_categories": data.get("selected_categories", ["copyright", "character", "general"]),
            "multi_select_enabled": data.get("multi_select_enabled", False)
        }
        success = save_ui_settings(ui_settings)
        return web.json_response({"success": success})
    except Exception as e:
        logger.error(f"保存UI设置接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/translate_tag")
async def translate_tag_route(request):
    """翻译单个tag"""
    try:
        tag = request.query.get("tag", "").strip()
        if not tag:
            return web.json_response({"success": False, "error": "缺少tag参数"})
        
        translation = translation_system.translate_tag(tag)
        return web.json_response({
            "success": True,
            "tag": tag,
            "translation": translation
        })
    except Exception as e:
        logger.error(f"翻译tag接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.post("/danbooru_gallery/translate_tags_batch")
async def translate_tags_batch_route(request):
    """批量翻译tags"""
    try:
        data = await request.json()
        tags = data.get("tags", [])
        
        if not isinstance(tags, list):
            return web.json_response({"success": False, "error": "tags必须是数组"})
        
        translations = translation_system.translate_tags_batch(tags)
        return web.json_response({
            "success": True,
            "translations": translations
        })
    except Exception as e:
        logger.error(f"批量翻译tags接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/search_chinese")
async def search_chinese_route(request):
    """中文搜索匹配 - 优先使用FTS5数据库搜索"""
    try:
        query = request.query.get("query", "").strip()
        limit = int(request.query.get("limit", "10"))
        if not query:
            return web.json_response({"success": True, "results": []})
        # 加载配置
        config = load_autocomplete_config()
        # ✅ 优先使用FTS5数据库搜索（速度更快，10-50ms → 2-5ms）
        if get_db_manager and config['cache'].get('use_database_query', True):
            try:
                db = get_db_manager()
                db_results = await db.search_tags_optimized(query, limit, search_type="chinese")
                if db_results:
                    # 转换为前端期望的格式
                    formatted_results = [
                        {
                            'tag': tag['tag'],
                            'translation_cn': tag.get('translation_cn'),
                            'category': tag['category'],
                            'post_count': tag['post_count'],
                            'match_score': tag.get('match_score', 5)
                        }
                        for tag in db_results
                    ]
                    logger.debug(f"[SearchChinese] FTS5数据库查询: '{query}' -> {len(formatted_results)}条结果")
                    return web.json_response({
                        "success": True,
                        "query": query,
                        "results": formatted_results
                    })
            except Exception as e:
                logger.warning(f"[SearchChinese] FTS5查询失败: {e}，回退到translation_system")
        # ⚠️ Fallback: 使用旧的translation_system（线性搜索，较慢）
        try:
            results = translation_system.search_chinese_tags(query, limit)
            logger.debug(f"[SearchChinese] translation_system查询: '{query}' -> {len(results)}条结果")
            return web.json_response({
                "success": True,
                "query": query,
                "results": results
            })
        except Exception as e:
            logger.error(f"[SearchChinese] translation_system查询失败: {e}")
            return web.json_response({
                "success": False,
                "error": str(e)
            })
    except Exception as e:
        logger.error(f"中文搜索接口错误: {e}")
        return web.json_response({"success": False, "error": str(e)})

@PromptServer.instance.routes.get("/danbooru_gallery/autocomplete_with_translation")
async def get_autocomplete_with_translation(request):
    """带翻译的自动补全API - 三层查询机制：数据库 → API → 空结果"""
    try:
        query = request.query.get("query", "")
        limit = int(request.query.get("limit", "20"))
        if not query:
            return web.json_response([])
        # 加载配置
        config = load_autocomplete_config()
        # ✅ 第1层：查询本地SQLite数据库（已包含翻译）
        if get_db_manager and config['cache'].get('use_database_query', True):
            try:
                db = get_db_manager()
                db_results = await db.search_tags_by_prefix(query, limit)
                if db_results:
                    # 数据库有结果，转换格式（已包含translation_cn）
                    formatted_results = [
                        {
                            'name': tag['tag'],
                            'category': tag['category'],
                            'post_count': tag['post_count'],
                            'translation': tag.get('translation_cn'),
                            'aliases': tag.get('aliases', [])
                        }
                        for tag in db_results
                    ]
                    logger.debug(f"[AutocompleteTranslation] 数据库查询成功: '{query}' -> {len(formatted_results)}条结果")
                    return web.json_response(formatted_results)
                else:
                    logger.debug(f"[AutocompleteTranslation] 数据库无结果: '{query}'")
            except Exception as e:
                logger.warning(f"[AutocompleteTranslation] 数据库查询失败: {e}，尝试API fallback")
        # ✅ 第2层：Fallback到Danbooru API（需要手动添加翻译）
        if config['offline_mode'].get('fallback_to_remote', True):
            try:
                timeout = config['offline_mode'].get('remote_timeout_ms', 2000) / 1000.0
                tags_url = f"{BASE_URL}/tags.json"
                params = {
                    "search[name_or_alias_matches]": f"{query}*",
                    "search[order]": "count",
                    "limit": limit
                }
                username, api_key = load_user_auth()
                auth = HTTPBasicAuth(username, api_key) if username and api_key else None
                logger.debug(f"[AutocompleteTranslation] 调用远程API: '{query}' (超时: {timeout}s)")
                response = requests.get(tags_url, params=params, auth=auth, timeout=timeout)
                response.raise_for_status()
                result = response.json()
                # 为每个tag添加翻译
                if isinstance(result, list):
                    for tag_data in result:
                        tag_name = tag_data.get('name', '')
                        translation = translation_system.translate_tag(tag_name)
                        tag_data['translation'] = translation
                    logger.info(f"[AutocompleteTranslation] API查询成功: '{query}' -> {len(result)}条结果")
                return web.json_response(result)
            except requests.Timeout:
                logger.warning(f"[AutocompleteTranslation] 远程API超时 (>{timeout}s): '{query}'")
            except requests.exceptions.RequestException as e:
                logger.warning(f"[AutocompleteTranslation] 远程API失败: {e}")
            except Exception as e:
                logger.error(f"[AutocompleteTranslation] API调用错误: {e}")
        # ✅ 第3层：返回空结果
        logger.debug(f"[AutocompleteTranslation] 所有查询方式均无结果: '{query}'")
        return web.json_response([])
    except Exception as e:
        logger.error(f"[AutocompleteTranslation] 处理请求时发生错误: {e}")
        return web.json_response([])

# ================================
# 核心节点（删除尺寸输出后）
# ================================
class DanbooruGalleryNode:
    _post_cache = {}

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                # 加载模式开关，默认同步（和改造前一致）
                "加载模式": (
                    ["同步加载（直接出原图）", "异步加载（先出提示词）"],
                    {"default": "同步加载（直接出原图）", "description": "选择是否异步加载图像"}
                ),
            },
            "optional": {},
            "hidden": {
                "selection_data": ("STRING", {"default": "{}", "multiline": True, "forceInput": True}),
            },
        }

    # 输出调整：删除尺寸输出，仅保留「提示词、图像、任务ID」
    RETURN_TYPES = ("STRING", "IMAGE", "STRING", "STRING", "STRING")  # 提示词、图像、任务ID
    RETURN_NAMES = ("提示词", "图像", "异步任务ID", "角色Tags", "画师Tags")
    OUTPUT_IS_LIST = (True, True, False, True, True)  # 提示词/图像是列表，任务ID是单个字符串
    FUNCTION = "get_selected_data"
    CATEGORY = "danbooru"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, selection_data="{}", 加载模式="同步加载（直接出原图）", **kwargs):
        # 加载模式变化时也触发节点更新
        return (selection_data, 加载模式)

    # 生成友好占位图的工具函数
    def _create_placeholder_image(self, width=512, height=512, text="加载中..."):
        """生成带文字提示的友好占位图（浅灰背景+文字）"""
        # 浅灰色背景（RGB: 240, 240, 240）
        img = Image.new("RGB", (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        
        # 适配不同系统字体（优先中文支持）
        font = None
        font_size = min(width, height) // 10  # 字体大小随图像尺寸自适应
        try:
            if sys.platform == "win32":
                font = ImageFont.truetype("simhei.ttf", size=font_size)  # Windows黑体
            elif sys.platform == "darwin":
                font = ImageFont.truetype("Arial Unicode.ttf", size=font_size)  # Mac兼容字体
            else:
                font = ImageFont.truetype("DejaVu-Sans.ttf", size=font_size)  # Linux兼容字体
        except Exception:
            font = ImageFont.load_default(size=font_size)  # 兜底默认字体
        
        # 文字居中绘制
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        text_x = (width - text_width) // 2
        text_y = (height - text_height) // 2
        draw.text((text_x, text_y), text, font=font, fill=(100, 100, 100))  # 深灰色文字
        
        # 转换为torch张量（格式：1, height, width, 3）
        img_array = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(img_array)[None, ...]

    def get_selected_data(self, selection_data="{}", 加载模式="同步加载（直接出原图）", **kwargs):
        """
        兼容两种模式：
        1. 同步加载（默认）：和改造前一致，直接输出原图+提示词
        2. 异步加载（可选）：先输出提示词+友好占位图+任务ID，后台加载原图
        """
        if not selection_data or selection_data == "{}":
            return (
                [""],  # 提示词
                [self._create_placeholder_image(512, 512, "无图像")],  # 图像（友好占位图）
                "empty_task",  # 任务ID
                [""],  # 角色Tags
                [""]   # 画师Tags
            )

        prompts = []
        image_urls = []
        characters = []
        artists = []
        task_id = f"danbooru_task_{id(selection_data)}_{int(time.time() * 1000)}"

        try:
            data = json.loads(selection_data)
            selections = data.get("selections", [])
            if not selections:
                return (
                    [""], 
                    [self._create_placeholder_image(512, 512, "无图像")], 
                    "empty_task"
                )

            # 第一步：解析提示词和URL（删除尺寸解析逻辑）
            for idx, sel in enumerate(selections):
                # 收集提示词
                prompts.append(sel.get("prompt", "").strip())
                # 收集图像URL
                image_url = sel.get("image_url")
                image_urls.append(image_url)
                characters.append(sel.get("character_tags", "").strip())
                artists.append(sel.get("artist_tags", "").strip())

            # ================================
            # 模式1：同步加载（默认，和改造前一致）
            # ================================
            if 加载模式 == "同步加载（直接出原图）":
                original_images = []
                for idx, url in enumerate(image_urls):
                    if url:
                        try:
                            # 同步下载并加载原图（和改造前逻辑完全一致）
                            with urllib.request.urlopen(url, timeout=15) as response:
                                img_data = response.read()
                            img = Image.open(io.BytesIO(img_data)).convert("RGB")
                            img_array = np.array(img).astype(np.float32) / 255.0
                            original_images.append(torch.from_numpy(img_array)[None, ...])
                        except Exception as e:
                            logger.error(f"同步加载图像失败 {url}: {e}")
                            # 加载失败时用友好占位图替代（而非黑图）
                            original_images.append(self._create_placeholder_image(512, 512, "加载失败"))
                    else:
                        original_images.append(self._create_placeholder_image(512, 512, "无URL"))
                # 返回：提示词+原图+空任务ID（空ID不影响使用）
                return (prompts, original_images, "sync_task", characters, artists)

            # ================================
            # 模式2：异步加载（可选，先出提示词）
            # ================================
            else:
                # 初始化缓存（使用默认尺寸512x512，或从URL获取真实尺寸）
                with cache_lock:
                    image_cache[task_id] = {
                        "create_time": time.time(),
                        "total": len(selections),
                        "images": [{"loaded": False, "tensor": None} for _ in selections]
                    }

                # 后台异步加载原图（自动适配图像真实尺寸）
                def async_load_image(idx, url):
                    try:
                        if not url:
                            raise ValueError("无有效URL")
                        # 下载完整图像并获取真实尺寸
                        with urllib.request.urlopen(url, timeout=15) as response:
                            img_data = response.read()
                        img = Image.open(io.BytesIO(img_data)).convert("RGB")
                        img_array = np.array(img).astype(np.float32) / 255.0
                        tensor = torch.from_numpy(img_array)[None, ...]  # 保留原图尺寸
                        # 更新缓存
                        with cache_lock:
                            if task_id in image_cache and idx < len(image_cache[task_id]["images"]):
                                image_cache[task_id]["images"][idx]["tensor"] = tensor
                                image_cache[task_id]["images"][idx]["loaded"] = True
                    except Exception as e:
                        logger.error(f"异步加载失败（{idx}）{url}: {e}")
                        # 失败时用友好占位图（默认512x512）
                        fail_placeholder = self._create_placeholder_image(512, 512, "加载失败")
                        with cache_lock:
                            if task_id in image_cache and idx < len(image_cache[task_id]["images"]):
                                image_cache[task_id]["images"][idx]["tensor"] = fail_placeholder
                                image_cache[task_id]["images"][idx]["loaded"] = True

                # 提交异步任务
                for idx, url in enumerate(image_urls):
                    executor.submit(async_load_image, idx, url)

                # 生成友好占位图（默认512x512，或根据URL快速获取尺寸）
                placeholders = []
                for url in image_urls:
                    if url:
                        try:
                            # 快速获取真实尺寸（仅下载前10KB头部，不影响性能）
                            with urllib.request.urlopen(url, timeout=3) as response:
                                img_header = io.BytesIO(response.read(1024 * 10))
                                with Image.open(img_header) as img:
                                    w, h = img.size
                                    placeholders.append(self._create_placeholder_image(w, h, "加载中..."))
                        except Exception:
                            placeholders.append(self._create_placeholder_image(512, 512, "加载中..."))
                    else:
                        placeholders.append(self._create_placeholder_image(512, 512, "无URL"))
                
                # 返回：提示词+占位图+任务ID
                return (prompts, placeholders, task_id, characters, artists)

        except Exception as e:
            logger.error(f"处理选中数据失败: {e}", exc_info=True)
            return (
                [""], 
                [self._create_placeholder_image(512, 512, "处理失败")], 
                "error_task",
                [""],
                [""]
            )
    
    @staticmethod
    def get_posts_internal(tags: str, limit: int = 100, page: int = 1, rating: str = None):
        settings = load_settings()
        cache_enabled = settings.get("cache_enabled", True)
        max_cache_age = settings.get("max_cache_age", 3600)
        # 创建缓存键
        cache_key = f"{tags}:{limit}:{page}:{rating}"
        # 如果启用了缓存，则检查缓存
        if cache_enabled:
            if cache_key in DanbooruGalleryNode._post_cache:
                cached_data, timestamp = DanbooruGalleryNode._post_cache[cache_key]
                if time.time() - timestamp < max_cache_age:
                    return (cached_data,)
        posts_url = f"{BASE_URL}/posts.json"
        
        # 分离 date: 标签和其他标签
        date_tag = ''
        other_tags = []
        for tag in tags.split(' '):
            if tag.strip().startswith('date:'):
                date_tag = tag.strip()
            elif tag.strip():
                other_tags.append(tag.strip())
        # 限制其他标签的数量
        if len(other_tags) > 2:
            other_tags = other_tags[:2]
        
        # 重新组合标签
        final_tags = ' '.join(other_tags)
        if date_tag:
            final_tags = f"{final_tags} {date_tag}".strip()
        if rating and rating.lower() != 'all':
            final_tags = f"{final_tags} rating:{rating}".strip()
        
        tags = final_tags
        
        username, api_key = load_user_auth()
        auth = HTTPBasicAuth(username, api_key) if username and api_key else None
        params = {
            "tags": tags.strip(),
            "limit": limit,
            "page": page,
        }
        
        try:
            response = requests.get(posts_url, params=params, auth=auth, timeout=15)
            response.raise_for_status()
            
            result_text = response.text
            
            # 如果启用了缓存，则存储结果
            if cache_enabled:
                DanbooruGalleryNode._post_cache[cache_key] = (result_text, time.time())
                # 清理旧缓存（可选，防止内存无限增长）
                if len(DanbooruGalleryNode._post_cache) > 200:  # 假设最多缓存200个请求
                    oldest_key = min(DanbooruGalleryNode._post_cache.keys(), key=lambda k: DanbooruGalleryNode._post_cache[k][1])
                    del DanbooruGalleryNode._post_cache[oldest_key]
            
            return (result_text,)
        except requests.exceptions.RequestException as e:
            logger.error(f"网络请求时发生错误: {e}")
            return ("[]",)
        except Exception as e:
            logger.error(f"发生未知错误: {e}")
            return ("[]",)

# ================================
# 辅助节点：异步图像加载器（适配删除尺寸后的逻辑）
# ================================
class DanbooruAsyncImageLoader:
    """辅助节点：通过任务ID获取异步加载完成的真实图像"""
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "异步任务ID": ("STRING", {"default": "", "description": "从D站画廊节点获取的任务ID"}),
                "超时时间(秒)": ("INT", {"default": 30, "min": 5, "max": 300, "step": 5}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("加载完成的图像",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "load_async_images"
    CATEGORY = "danbooru"

    def load_async_images(self, 异步任务ID, 超时时间_秒):
        if 异步任务ID in ["empty_task", "error_task", "sync_task", ""]:
            logger.warning("无效/同步模式的任务ID，返回友好占位图")
            return ([self._create_placeholder_image(512, 512, "无效ID")],)

        start_time = time.time()
        total_images = 0

        # 检查任务存在性
        with cache_lock:
            if 异步任务ID not in image_cache:
                logger.error(f"任务ID {异步任务ID} 不存在")
                return ([self._create_placeholder_image(512, 512, "任务不存在")],)
            total_images = image_cache[异步任务ID]["total"]

        # 等待加载完成
        while time.time() - start_time < 超时时间_秒:
            with cache_lock:
                all_loaded = all(img["loaded"] for img in image_cache[异步任务ID]["images"])
                if all_loaded:
                    break
            time.sleep(0.5)

        # 收集图像（保留原图真实尺寸）
        with cache_lock:
            task_data = image_cache.get(异步任务ID, {})
            images = []
            for img_info in task_data.get("images", []):
                # 优先用加载好的原图（保留真实尺寸），无则用友好占位图
                tensor = img_info.get("tensor") or self._create_placeholder_image(512, 512, "加载超时")
                images.append(tensor)
            # 清理缓存
            if 异步任务ID in image_cache:
                del image_cache[异步任务ID]

        return (images,)

    # 复用友好占位图生成函数
    def _create_placeholder_image(self, width=512, height=512, text="加载中..."):
        img = Image.new("RGB", (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)
        font_size = min(width, height) // 10
        font = None
        try:
            if sys.platform == "win32":
                font = ImageFont.truetype("simhei.ttf", size=font_size)
            elif sys.platform == "darwin":
                font = ImageFont.truetype("Arial Unicode.ttf", size=font_size)
            else:
                font = ImageFont.truetype("DejaVu-Sans.ttf", size=font_size)
        except Exception:
            font = ImageFont.load_default(size=font_size)
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_x = (width - (text_bbox[2] - text_bbox[0])) // 2
        text_y = (height - (text_bbox[3] - text_bbox[1])) // 2
        draw.text((text_x, text_y), text, font=font, fill=(100, 100, 100))
        img_array = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(img_array)[None, ...]

# ================================
# 节点映射（更新）
# ================================
def get_node_class_mappings():
    return {
        "DanbooruGalleryNode": DanbooruGalleryNode,
        "DanbooruAsyncImageLoader": DanbooruAsyncImageLoader  # 新增辅助节点
    }

def get_node_display_name_mappings():
    return {
        "DanbooruGalleryNode": "D站画廊 (Danbooru Gallery)",
        "DanbooruAsyncImageLoader": "D站异步图像加载器"  # 辅助节点显示名称
    }

NODE_CLASS_MAPPINGS = get_node_class_mappings()
NODE_DISPLAY_NAME_MAPPINGS = get_node_display_name_mappings()
