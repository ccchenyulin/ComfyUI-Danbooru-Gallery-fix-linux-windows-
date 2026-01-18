"""
Open In Krita节点 - 将图像发送到Krita进行编辑，并接收编辑后的图像和蒙版
支持Alpha通道保留/颜色填充选项
"""

import torch
import numpy as np
from PIL import Image
import tempfile
import time
import os
from pathlib import Path
from typing import Tuple, Optional

from server import PromptServer
from .krita_manager import get_manager
from .plugin_installer import KritaPluginInstaller
import comfy.model_management  # 用于检测ComfyUI取消执行
from ..utils.logger import get_logger

# 初始化logger
logger = get_logger(__name__)

# 插件启用提示信息
PLUGIN_ENABLE_HINT = """如果插件未生效，请检查：
1. 打开 Krita → Settings → Configure Krita
2. 进入 Python Plugin Manager
3. 勾选启用 "Open In Krita" 插件
4. 重启 Krita"""

# 存储节点等待接收的数据
_pending_data = {}

# 存储节点等待状态
_waiting_nodes = {}  # {node_id: {"waiting": True, "cancelled": False}}

# 跨系统路径映射配置（关键：Windows路径 → Linux路径）
WINDOWS_TO_LINUX_PATH_MAP = {
    "A:\\D\\open_in_krita\\": "/mnt/d/open_in_krita/",
    "A:/D/open_in_krita/": "/mnt/d/open_in_krita/"  # 兼容正斜杠格式
}


class FetchFromKrita:
    """
    从Krita获取数据节点
    支持Alpha通道保留/颜色填充，不丢弃透明信息
    """

    # 类变量：跟踪当前在Krita中的图像
    _current_image_hash = None
    _current_temp_file = None

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "active": ("BOOLEAN", {
                    "default": True,
                    "label_on": "启用",
                    "label_off": "禁用"
                }),
                "max_wait_time": ("FLOAT", {
                    "default": 3600.0,
                    "min": 60.0,
                    "max": 86400.0,
                    "step": 60.0,
                    "tooltip": "最长等待时间（秒）：60秒-24小时，默认1小时"
                }),
                "alpha_handling": (
                    ["无填充（保留透明）", "白色填充", "黑色填充", "灰色填充"],
                    {"default": "无填充（保留透明）", "tooltip": "处理图像Alpha通道的方式"}
                ),
            },
            "optional": {
                "mask": ("MASK",),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    FUNCTION = "process"
    CATEGORY = "danbooru"
    OUTPUT_NODE = False

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """
        强制节点每次都重新执行，避免ComfyUI缓存
        返回当前时间戳，确保每次执行都被视为"改变"
        """
        import time
        return time.time()

    def __init__(self):
        self.manager = get_manager()
        # 跨系统共享目录（Linux侧路径，需与Windows侧A:\D\open_in_krita对应）
        self.temp_dir = Path("/mnt/d/open_in_krita")
        self.temp_dir.mkdir(exist_ok=True)
        # 记录上次open请求（避免重复）
        self._last_open_request = {}

    def _convert_windows_path_to_linux(self, windows_path: str) -> str:
        """
        将Krita返回的Windows路径转换为Linux路径
        Args:
            windows_path: Krita返回的Windows格式路径（如 A:\D\open_in_krita\XXX.png）
        Returns:
            str: Linux格式路径（如 /mnt/d/open_in_krita/XXX.png）
        """
        if not windows_path:
            return ""
        
        # 1. 统一路径分隔符为正斜杠（兼容Windows的反斜杠）
        path = windows_path.replace("\\", "/")
        logger.debug(f"路径转换前（统一分隔符）: {path}")
        
        # 2. 替换路径前缀（Windows盘符 → Linux挂载目录）
        for win_prefix, linux_prefix in WINDOWS_TO_LINUX_PATH_MAP.items():
            if path.startswith(win_prefix.replace("\\", "/")):  # 兼容前缀中的分隔符
                path = path.replace(win_prefix.replace("\\", "/"), linux_prefix)
                break
        
        # 3. 处理大小写（Linux路径区分大小写，确保挂载目录匹配）
        path = path.lower().replace("/mnt/d/open_in_krita/", "/mnt/d/open_in_krita/")
        logger.info(f"路径转换完成：Windows路径 → Linux路径")
        logger.info(f"  原始路径: {windows_path}")
        logger.info(f"  转换后: {path}")
        
        return path

    def _get_fill_color(self, alpha_handling: str) -> Tuple[int, int, int]:
        """根据选择返回填充颜色（RGB）"""
        if alpha_handling == "白色填充":
            return (255, 255, 255)
        elif alpha_handling == "黑色填充":
            return (0, 0, 0)
        elif alpha_handling == "灰色填充":
            return (128, 128, 128)
        else:  # 无填充，返回默认（实际不会用到）
            return (0, 0, 0)

    def _handle_alpha_channel(self, pil_image: Image.Image, alpha_handling: str) -> Image.Image:
        """
        处理Alpha通道：保留透明或填充颜色
        Args:
            pil_image: 原始PIL图像（可能含Alpha通道）
            alpha_handling: 处理方式（无填充/白色/黑色/灰色填充）
        Returns:
            Image.Image: 处理后的图像（RGBA或RGB）
        """
        # 如果图像没有Alpha通道，直接返回
        if pil_image.mode != "RGBA":
            logger.debug(f"图像无Alpha通道（模式：{pil_image.mode}），直接返回")
            return pil_image.convert("RGB") if alpha_handling != "无填充（保留透明）" else pil_image

        logger.debug(f"处理Alpha通道：{alpha_handling}（原始模式：RGBA）")
        
        # 保留透明：直接返回RGBA图像
        if alpha_handling == "无填充（保留透明）":
            return pil_image
        
        # 颜色填充：在纯色背景上合成图像
        fill_color = self._get_fill_color(alpha_handling)
        # 创建与原图像尺寸相同的纯色背景（RGB模式）
        background = Image.new("RGB", pil_image.size, fill_color)
        # 使用Alpha通道作为蒙版，将原图像合成到背景上
        background.paste(pil_image, (0, 0), pil_image)
        return background

    def _get_final_mask(self, krita_mask: Optional[torch.Tensor], input_mask: Optional[torch.Tensor],
                        image_shape: Tuple[int, ...]) -> torch.Tensor:
        """
        决定最终返回的mask，遵循优先级规则

        优先级：krita_mask > input_mask > empty_mask

        Args:
            krita_mask: 从Krita返回的蒙版
            input_mask: 节点的蒙版输入
            image_shape: 图像形状 (B, H, W)，用于创建空蒙版

        Returns:
            torch.Tensor: 最终的蒙版张量 [B, H, W]
        """
        # 优先使用Krita返回的mask（如果有效）
        if krita_mask is not None and not torch.all(krita_mask == 0):
            return krita_mask

        # 其次使用输入的mask
        if input_mask is not None:
            return input_mask

        # 最后返回空mask
        return torch.zeros(image_shape)

    def _is_krita_running(self) -> bool:
        """跨系统检测Krita是否运行：通过共享目录的插件标志文件 + check_document请求"""
        logger.info(f"===== 开始跨系统检测Krita运行状态 =====")
        logger.info(f"共享目录路径（节点侧）: {self.temp_dir}")
        
        # 1. 优先检查Krita插件加载标志（最可靠）
        plugin_loaded_flag = self.temp_dir / "_plugin_loaded.txt"
        logger.info(f"检查插件标志文件: {plugin_loaded_flag}")
        
        if plugin_loaded_flag.exists():
            try:
                # 读取标志文件前100字符验证
                flag_content = plugin_loaded_flag.read_text(encoding='utf-8')[:100]
                logger.info(f"✓ 找到插件标志文件，确认Krita已运行（跨系统）")
                logger.info(f"标志文件内容预览: {flag_content}...")
                return True
            except Exception as e:
                logger.error(f"× 读取插件标志文件失败: {str(e)}")
        
        logger.warning(f"× 未找到插件标志文件（可能是文件未同步或插件未加载）")
        
        # 2. 兜底：发送check_document请求验证
        try:
            logger.info(f"发送check_document请求兜底检测...")
            temp_node_id = f"check_running_{int(time.time())}"
            check_result = self._check_krita_has_document(temp_node_id)
            logger.info(f"check_document请求结果: {check_result}")
            return check_result
        except Exception as e:
            logger.error(f"× check_document请求失败: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
            return False

    def _wait_for_krita_start(self, max_wait: float = 30.0) -> bool:
        """等待Krita启动（跨系统版本：等待共享目录标志或请求响应）"""
        logger.info(f"等待Krita启动（最大{max_wait}秒，跨系统模式）...")
        elapsed = 0
        check_interval = 0.5

        while elapsed < max_wait:
            if self._is_krita_running():
                logger.info(f"✓ Krita已启动（跨系统，耗时{elapsed:.1f}秒）")
                return True
            time.sleep(check_interval)
            elapsed += check_interval
            logger.debug(f"等待中... 已耗时{elapsed:.1f}秒")

        logger.warning(f"✗ Krita启动超时（{max_wait}秒），请确认Windows上Krita已启动且插件已启用")
        return False

    def _get_image_hash(self, image: torch.Tensor) -> str:
        """计算图像内容的hash值"""
        import hashlib
        return hashlib.md5(image.cpu().numpy().tobytes()).hexdigest()

    def _check_krita_has_document(self, unique_id: str) -> bool:
        """
        通过文件通信检查Krita是否有活动文档（跨系统兼容，延长等待时间）

        Args:
            unique_id: 节点ID

        Returns:
            bool: True表示有活动文档, False表示无活动文档或检查失败
        """
        try:
            timestamp = int(time.time() * 1000)
            request_file = self.temp_dir / f"check_document_{unique_id}_{timestamp}.request"
            response_file = self.temp_dir / f"check_document_{unique_id}_{timestamp}.response"

            # 创建请求文件
            with open(request_file, 'w', encoding='utf-8') as f:
                f.write(f"{unique_id}\n{timestamp}\n")
            logger.info(f"✓ 创建check_document请求文件: {request_file.name}")

            # 延长等待时间到10秒（适配跨系统文件同步延迟）
            max_wait = 10.0
            check_interval = 0.5
            elapsed = 0

            while elapsed < max_wait:
                if response_file.exists():
                    logger.info(f"✓ 检测到check_document响应文件: {response_file.name}")
                    time.sleep(0.2)  # 延长等待，确保文件完全写入
                    break
                time.sleep(check_interval)
                elapsed += check_interval
                logger.debug(f"等待响应中... 已耗时{elapsed:.1f}秒")

            if not response_file.exists():
                logger.warning(f"× check_document响应超时（{max_wait}秒）")
                # 清理请求文件
                try:
                    request_file.unlink(missing_ok=True)
                except:
                    pass
                return False

            # 读取响应
            import json
            with open(response_file, 'r', encoding='utf-8') as f:
                response_data = json.load(f)

            has_document = response_data.get("has_active_document", False)
            logger.info(f"check_document响应结果: Krita是否有活动文档 = {has_document}")

            # 清理文件
            try:
                request_file.unlink(missing_ok=True)
                response_file.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"× 清理check_document文件失败: {str(e)}")

            return has_document

        except Exception as e:
            logger.error(f"× check_document请求执行失败: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
            return False

    def process(self, image: torch.Tensor, active: bool, max_wait_time: float, alpha_handling: str, 
                unique_id: str, mask: Optional[torch.Tensor] = None):
        """
        处理节点执行（新增alpha_handling参数处理Alpha通道）

        Args:
            image: 输入图像张量 [B, H, W, C]
            active: 是否启用（False时直接返回输入）
            max_wait_time: 最长等待时间（秒），范围60-86400
            alpha_handling: Alpha通道处理方式
            unique_id: 节点唯一ID
            mask: 可选的蒙版输入 [B, H, W]，作为后备蒙版使用

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: (编辑后的图像, 蒙版)
        """
        logger.debug(f"Node {unique_id} processing (active={active}, alpha_handling={alpha_handling})")

        # 如果未启用，直接返回输入图像和蒙版（使用输入mask或空mask）
        if not active:
            logger.debug(f"Node disabled, passing through")
            final_mask = self._get_final_mask(None, mask, (image.shape[0], image.shape[1], image.shape[2]))
            return (image, final_mask)

        # ===== 第一步：版本检查和自动更新 =====
        try:
            installer = KritaPluginInstaller()

            if installer.needs_update():
                source_version = installer.source_version
                installed_version = installer.get_installed_version()

                logger.warning(f"⚠️ Plugin update needed!")
                logger.debug(f"  Source version: {source_version}")
                logger.debug(f"  Installed version: {installed_version}")

                # Toast提示：检测到更新（无论Krita是否运行都显示）
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": f"🔄 检测到插件更新 ({installed_version} → {source_version})\n正在更新插件...",
                    "type": "info"
                })

                # 检查Krita是否正在运行（跨系统检测）
                krita_running = self._is_krita_running()

                if krita_running:
                    logger.debug(f"Krita is running, killing process for plugin update...")
                    # 杀掉Krita进程
                    installer.kill_krita_process()
                    time.sleep(1.5)  # 等待进程完全结束

                # 重新安装插件
                logger.debug(f"Installing updated plugin...")
                success = installer.install_plugin(force=True)

                if success:
                    logger.info(f"✓ Plugin updated to v{source_version}")

                    # Toast提示：更新成功（包含启用说明）
                    PromptServer.instance.send_sync("open-in-krita-notification", {
                        "node_id": unique_id,
                        "message": f"✓ Krita插件已更新到 v{source_version}\n请重启 Krita 后再次执行工作流\n\n{PLUGIN_ENABLE_HINT}",
                        "type": "success"
                    })

                    logger.debug(f"Plugin updated, execution stopped. User must execute again.")

                    # 🔥 抛出异常，中断执行流程
                    raise RuntimeError(f"✓ Krita插件已更新到 v{source_version}，请重新执行工作流")
                else:
                    logger.warning(f"✗ Plugin update failed")
                    PromptServer.instance.send_sync("open-in-krita-notification", {
                        "node_id": unique_id,
                        "message": f"⚠️ Krita插件更新失败\n请检查日志",
                        "type": "error"
                    })

                    # 🔥 抛出异常，中断执行流程
                    raise RuntimeError("⚠️ Krita插件更新失败，请检查日志")
            else:
                logger.debug(f"Plugin version check OK: v{installer.source_version}")

        except Exception as e:
            logger.error(f"Version check error: {e}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")

        # ===== 第二步：确保Krita插件已安装（兼容性检查，正常情况下版本检查已处理） =====
        try:
            installer = KritaPluginInstaller()
            if not installer.check_plugin_installed():
                logger.info("Installing Krita plugin...")
                
                # Toast提示：开始安装插件
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": f"📦 正在安装Krita插件 v{installer.source_version}...",
                    "type": "info"
                })
                
                success = installer.install_plugin()
                
                if success:
                    logger.info(f"✓ Plugin installed successfully: v{installer.source_version}")
                    # Toast提示：安装成功（包含启用说明）
                    PromptServer.instance.send_sync("open-in-krita-notification", {
                        "node_id": unique_id,
                        "message": f"✓ Krita插件已安装 v{installer.source_version}\n\n{PLUGIN_ENABLE_HINT}",
                        "type": "success"
                    })
                else:
                    logger.warning(f"✗ Plugin installation failed")
                    # Toast提示：安装失败
                    PromptServer.instance.send_sync("open-in-krita-notification", {
                        "node_id": unique_id,
                        "message": "⚠️ Krita插件安装失败\n请检查日志",
                        "type": "warning"
                    })
        except Exception as e:
            logger.error(f"Plugin installation error: {e}")
            # 发送警告Toast
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": f"⚠️ Krita插件安装失败: {str(e)}\n部分功能可能不可用",
                "type": "warning"
            })

        # ===== 第三步：重新检测Krita是否运行（关键修复：跨系统兼容）=====
        logger.info(f"===== 重新检测Krita运行状态（跨系统）=====")
        krita_running = self._is_krita_running()  # 强制重新检测
        logger.info(f"Krita运行状态检测结果: {krita_running}")

        if not krita_running:
            # 尝试等待Krita启动（给跨系统同步时间）
            logger.info(f"Krita未检测到运行，尝试等待30秒...")
            krita_running = self._wait_for_krita_start(max_wait=30.0)
            if not krita_running:
                logger.info(f"等待超时，使用默认图像")
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": "ℹ️ Krita未运行（跨系统检测失败）或插件未启用\n请确认：\n1. Windows上Krita已启动\n2. 插件已在Krita中启用\n3. 共享目录/mnt/d/open_in_krita可访问",
                    "type": "info"
                })
                final_mask = self._get_final_mask(None, mask, (image.shape[0], image.shape[1], image.shape[2]))
                return (image, final_mask)

        # ===== 第四步：直接从Krita获取数据（跨系统通信）=====
        logger.info(f"Krita已运行，开始发送fetch请求...")
        logger.info(f"发送fetch请求到共享目录: {self.temp_dir}")

        # 创建fetch请求并等待响应
        timestamp = int(time.time() * 1000)
        request_file = self.temp_dir / f"fetch_{unique_id}_{timestamp}.request"
        response_file = self.temp_dir / f"fetch_{unique_id}_{timestamp}.response"

        # 创建请求文件
        try:
            with open(request_file, 'w', encoding='utf-8') as f:
                f.write(f"{unique_id}\n{timestamp}\n")
            logger.info(f"✓ Fetch request created: {request_file.name}")
        except Exception as e:
            logger.error(f"× Error creating request file: {e}")
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": "ℹ️ 创建请求文件失败，使用默认图像",
                "type": "info"
            })
            final_mask = self._get_final_mask(None, mask, (image.shape[0], image.shape[1], image.shape[2]))
            return (image, final_mask)

        # 等待响应文件（延长等待时间到15秒，适配跨系统）
        logger.info(f"等待Krita响应（最大15秒）...")
        max_wait = 15.0
        check_interval = 0.2
        elapsed = 0

        while elapsed < max_wait:
            if response_file.exists():
                logger.info(f"✓ Response file detected: {response_file.name}")
                time.sleep(0.3)  # 延长等待，确保文件完全写入
                break
            time.sleep(check_interval)
            elapsed += check_interval
            logger.debug(f"等待响应中... 已耗时{elapsed:.1f}秒")

        if not response_file.exists():
            logger.warning(f"× Krita response timeout ({max_wait}秒)")
            # 清理请求文件
            try:
                request_file.unlink(missing_ok=True)
            except:
                pass
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": f"⚠️ Krita响应超时，使用默认图像\n\n{PLUGIN_ENABLE_HINT}",
                "type": "warning"
            })
            final_mask = self._get_final_mask(None, mask, (image.shape[0], image.shape[1], image.shape[2]))
            return (image, final_mask)

        # 读取响应
        try:
            import json
            with open(response_file, 'r', encoding='utf-8') as f:
                response_data = json.load(f)

            logger.debug(f"Response data: {response_data}")

            if response_data.get("status") != "success":
                raise Exception(f"Response status is not success: {response_data.get('status')}")

            # 关键修复：将Krita返回的Windows路径转为Linux路径
            image_path_str = response_data.get("image_path")
            mask_path_str = response_data.get("mask_path")

            if not image_path_str:
                raise Exception("No image_path in response")

            # 转换图像路径（Windows → Linux）
            linux_image_path_str = self._convert_windows_path_to_linux(image_path_str)
            image_path = Path(linux_image_path_str)

            # 转换蒙版路径（Windows → Linux）
            linux_mask_path_str = self._convert_windows_path_to_linux(mask_path_str) if mask_path_str else None

            # 加载图像（添加重试逻辑，应对跨系统文件同步延迟）
            max_retry = 3
            retry_count = 0
            result_image = None
            while retry_count < max_retry and not image_path.exists():
                logger.warning(f"× 图像文件暂未找到，重试中（{retry_count+1}/{max_retry}）: {image_path}")
                time.sleep(1.0)  # 等待1秒后重试
                retry_count += 1

            if not image_path.exists():
                raise Exception(f"Image file not found after {max_retry} retries: {image_path}")
            
            # 加载图像并处理Alpha通道
            result_image = self._load_image_from_file(image_path, alpha_handling)

            # 加载蒙版（如果有）
            result_mask = torch.zeros((1, result_image.shape[1], result_image.shape[2]))
            if linux_mask_path_str:
                mask_path = Path(linux_mask_path_str)
                # 蒙版文件重试逻辑
                retry_count = 0
                while retry_count < max_retry and not mask_path.exists():
                    logger.warning(f"× 蒙版文件暂未找到，重试中（{retry_count+1}/{max_retry}）: {mask_path}")
                    time.sleep(1.0)
                    retry_count += 1
                
                if mask_path.exists():
                    result_mask = self._load_mask_from_file(mask_path)
                else:
                    logger.warning(f"Mask file not found after {max_retry} retries: {mask_path}, using empty mask")

            # 清理文件
            try:
                request_file.unlink(missing_ok=True)
                response_file.unlink(missing_ok=True)
                # 可选：清理Krita导出的临时图像文件（如果需要）
                if image_path.exists() and image_path.parent == self.temp_dir:
                    image_path.unlink(missing_ok=True)
                if linux_mask_path_str and Path(linux_mask_path_str).exists():
                    Path(linux_mask_path_str).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"× 清理临时文件失败: {e}")

            logger.info(f"✓ Successfully fetched data from Krita (跨系统通信成功，Alpha处理：{alpha_handling})")
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": f"✓ 已从Krita获取数据（跨系统通信成功，Alpha处理：{alpha_handling}）",
                "type": "success"
            })

            final_mask = self._get_final_mask(result_mask, mask, (1, result_image.shape[1], result_image.shape[2]))
            return (result_image, final_mask)

        except Exception as e:
            logger.error(f"× Error processing Krita response: {e}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")

            # 清理文件
            try:
                request_file.unlink(missing_ok=True)
                response_file.unlink(missing_ok=True)
            except:
                pass

            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": f"⚠️ 获取Krita数据失败: {str(e)}\n使用默认图像",
                "type": "warning"
            })
            final_mask = self._get_final_mask(None, mask, (image.shape[0], image.shape[1], image.shape[2]))
            return (image, final_mask)

    def _save_image_to_temp(self, image: torch.Tensor, unique_id: str) -> Optional[Path]:
        """
        保存图像到临时文件

        Args:
            image: 图像张量 [B, H, W, C]
            unique_id: 节点ID

        Returns:
            Path: 临时文件路径
        """
        try:
            # 🔥 新增：清理该节点的旧临时文件（防止Krita打开多个旧标签页）
            old_files = list(self.temp_dir.glob(f"comfyui_{unique_id}_*.png"))
            for old_file in old_files:
                try:
                    old_file.unlink()
                    logger.debug(f"Cleaned old temp file: {old_file.name}")
                except Exception as e:
                    logger.debug(f"Warning: Failed to delete old temp file {old_file.name}: {e}")

            # 取第一张图像（如果是batch）
            if image.dim() == 4:
                image = image[0]

            # 转换为numpy数组 [H, W, C]
            np_image = (image.cpu().numpy() * 255).astype(np.uint8)

            # 转换为PIL Image（根据通道数自动处理模式）
            if np_image.shape[-1] == 4:
                pil_image = Image.fromarray(np_image, mode="RGBA")
            else:
                pil_image = Image.fromarray(np_image).convert("RGB")

            # 保存到临时文件
            temp_file = self.temp_dir / f"comfyui_{unique_id}_{int(time.time())}.png"
            pil_image.save(str(temp_file), format='PNG')

            logger.debug(f"Saved temp image to shared dir: {temp_file}")
            return temp_file

        except Exception as e:
            logger.error(f"Error saving temp image: {e}")
            return None

    def _load_image_from_file(self, file_path: Path, alpha_handling: str) -> torch.Tensor:
        """
        从文件加载图像（支持Alpha通道处理）

        Args:
            file_path: 图像文件路径
            alpha_handling: Alpha通道处理方式
        Returns:
            torch.Tensor: 图像张量 [1, H, W, C]（C=3或4）
        """
        try:
            # 打开原始图像（保留所有通道）
            pil_image = Image.open(file_path)
            logger.debug(f"原始图像信息：路径={file_path.name}，模式={pil_image.mode}，尺寸={pil_image.size}")
            
            # 处理Alpha通道
            processed_image = self._handle_alpha_channel(pil_image, alpha_handling)
            
            # 转换为numpy数组
            np_image = np.array(processed_image).astype(np.float32) / 255.0
            
            # 转换为张量（添加batch维度）
            # 如果是RGBA模式（4通道），直接保留；否则为RGB（3通道）
            tensor = torch.from_numpy(np_image).unsqueeze(0)  # [1, H, W, C]
            
            logger.debug(f"加载图像完成：形状={tensor.shape}，模式={processed_image.mode}")
            return tensor
        except Exception as e:
            logger.error(f"Error loading image from {file_path}: {e}")
            raise

    def _load_mask_from_file(self, file_path: Path) -> torch.Tensor:
        """
        从文件加载蒙版

        Args:
            file_path: 蒙版文件路径

        Returns:
            torch.Tensor: 蒙版张量 [B, H, W]
        """
        try:
            pil_mask = Image.open(file_path).convert('L')  # 转换为灰度
            np_mask = np.array(pil_mask).astype(np.float32) / 255.0
            tensor = torch.from_numpy(np_mask).unsqueeze(0)  # [B, H, W]
            logger.debug(f"Loaded mask: {file_path.name}, shape: {tensor.shape}")
            return tensor
        except Exception as e:
            logger.error(f"Error loading mask from {file_path}: {e}")
            raise

    @staticmethod
    def load_image_from_bytes(image_bytes: bytes, alpha_handling: str = "无填充（保留透明）") -> torch.Tensor:
        """
        从字节数据加载图像（支持Alpha通道处理）

        Args:
            image_bytes: PNG图像字节数据
            alpha_handling: Alpha通道处理方式
        Returns:
            torch.Tensor: 图像张量 [1, H, W, C]（C=3或4）
        """
        import io
        pil_image = Image.open(io.BytesIO(image_bytes))
        
        # 处理Alpha通道
        fetch_instance = FetchFromKrita()
        processed_image = fetch_instance._handle_alpha_channel(pil_image, alpha_handling)
        
        np_image = np.array(processed_image).astype(np.float32) / 255.0
        tensor = torch.from_numpy(np_image).unsqueeze(0)  # [1, H, W, C]

        return tensor

    @staticmethod
    def load_mask_from_bytes(mask_bytes: bytes) -> torch.Tensor:
        """
        从字节数据加载蒙版

        Args:
            mask_bytes: PNG蒙版字节数据

        Returns:
            torch.Tensor: 蒙版张量 [B, H, W]
        """
        import io
        pil_mask = Image.open(io.BytesIO(mask_bytes))
        pil_mask = pil_mask.convert('L')  # 转换为灰度

        np_mask = np.array(pil_mask).astype(np.float32) / 255.0
        tensor = torch.from_numpy(np_mask).unsqueeze(0)  # [B, H, W]

        return tensor

    @staticmethod
    def set_pending_data(node_id: str, image: torch.Tensor, mask: torch.Tensor):
        """
        设置待处理数据（由API调用）

        Args:
            node_id: 节点ID
            image: 图像张量
            mask: 蒙版张量
        """
        _pending_data[node_id] = (image, mask)
        logger.debug(f"Set pending data for node {node_id}")

    @staticmethod
    def get_pending_data(node_id: str) -> Optional[Tuple[torch.Tensor, torch.Tensor]]:
        """获取待处理数据"""
        return _pending_data.get(node_id)

    @staticmethod
    def clear_pending_data(node_id: str):
        """清除待处理数据"""
        if node_id in _pending_data:
            del _pending_data[node_id]

    @staticmethod
    def cancel_waiting(node_id: str):
        """
        取消节点等待

        Args:
            node_id: 节点ID
        """
        if node_id in _waiting_nodes:
            _waiting_nodes[node_id]["cancelled"] = True
            logger.debug(f"Cancelled waiting for node {node_id}")

    def _create_open_request(self, image_path: Path, unique_id: str) -> bool:
        """
        创建open请求文件，通知Krita插件打开指定图像

        Args:
            image_path: 要打开的图像文件路径
            unique_id: 节点ID

        Returns:
            bool: 是否成功创建请求
        """
        try:
            # 检查是否在短时间内为同一图像创建过请求（避免重复打开）
            current_time = time.time()
            image_key = str(image_path.resolve())  # 使用绝对路径作为key

            if unique_id in self._last_open_request:
                last_image, last_time = self._last_open_request[unique_id]
                # 如果在5秒内为同一图像创建过请求，跳过
                if last_image == image_key and (current_time - last_time) < 5.0:
                    logger.warning(f"⚠ Skip duplicate open request (same image within 5s)")
                    logger.debug(f"Image: {image_path.name}")
                    logger.debug(f"Last request: {current_time - last_time:.1f}s ago")
                    return True  # 返回成功，避免重复创建

            # 记录本次请求
            self._last_open_request[unique_id] = (image_key, current_time)

            timestamp = int(time.time() * 1000)
            request_file = self.temp_dir / f"open_{unique_id}_{timestamp}.request"

            # 创建请求文件，包含图像路径
            import json
            request_data = {
                "image_path": str(image_path),
                "node_id": unique_id,
                "timestamp": timestamp
            }

            with open(request_file, 'w', encoding='utf-8') as f:
                json.dump(request_data, f, ensure_ascii=False, indent=2)

            logger.debug(f"===== Open Request Created =====")
            logger.debug(f"Request file: {request_file}")
            logger.debug(f"Node ID: {unique_id}")
            logger.debug(f"Image path: {image_path}")
            logger.debug(f"Timestamp: {timestamp}")
            logger.info(f"✓ Open request ready for Krita to process (共享目录: {self.temp_dir})")
            return True

        except Exception as e:
            logger.warning(f"✗ Failed to create open request: {e}")
            import traceback
            logger.debug(f"错误详情: {traceback.format_exc()}")
            return False


def get_node_class_mappings():
    """返回节点类映射"""
    return {
        "FetchFromKrita": FetchFromKrita
    }


def get_node_display_name_mappings():
    """返回节点显示名称映射"""
    return {
        "FetchFromKrita": "从Krita获取数据 (支持Alpha通道)"
    }


# 全局映射变量
NODE_CLASS_MAPPINGS = get_node_class_mappings()
NODE_DISPLAY_NAME_MAPPINGS = get_node_display_name_mappings()
