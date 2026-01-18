"""
Send To Krita节点 - 核心层级+强制透明+遮罩裁剪+对齐方式
仅保留：新建独立文档、当前文档最上层
Alpha处理写死为“无填充（保留透明）”
支持5种对齐方式，有遮罩时仅输出被选中部分
"""

import torch
import numpy as np
from PIL import Image
import time
import json
import os
from pathlib import Path
from typing import Tuple, Optional

from server import PromptServer
from .plugin_installer import KritaPluginInstaller
import comfy.model_management
from ..utils.logger import get_logger

# 初始化logger
logger = get_logger(__name__)

# 插件启用提示
PLUGIN_ENABLE_HINT = """如果Krita未接收图像，请检查：
1. 打开 Krita → Settings → Configure Krita
2. 进入 Python Plugin Manager
3. 勾选启用 "Open In Krita" 插件
4. 重启 Krita"""

# 跨系统路径映射（保持与现有逻辑一致）
WINDOWS_TO_LINUX_PATH_MAP = {
    "A:\\D\\open_in_krita\\": "/mnt/d/open_in_krita/",
    "A:/D/open_in_krita/": "/mnt/d/open_in_krita/"
}
LINUX_TO_WINDOWS_PATH_MAP = {v: k for k, v in WINDOWS_TO_LINUX_PATH_MAP.items()}

# 存储已发送请求（防抖用）
_sent_requests = {}


class SendToKrita:
    """仅核心层级+强制透明+遮罩裁剪+对齐方式支持"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),  # 输入图像张量 [B, H, W, C]
                "active": ("BOOLEAN", {
                    "default": True,
                    "label_on": "启用",
                    "label_off": "禁用"
                }),
                "auto_open": ("BOOLEAN", {
                    "default": True,
                    "label_on": "自动打开",
                    "label_off": "仅保存",
                    "tooltip": "是否让Krita自动处理图像"
                }),
                # 仅保留2个核心层级
                "layer_position": (
                    [
                        "新建独立文档",
                        "当前文档最上层"
                    ],
                    {"default": "新建独立文档", "tooltip": "选择图像发送到Krita的位置"}
                ),
                # 新增：对齐方式下拉菜单
                "alignment": (
                    [
                        "居中对齐",
                        "左上对齐",
                        "右上对齐",
                        "左下对齐",
                        "右下对齐"
                    ],
                    {"default": "居中对齐", "tooltip": "图像在目标尺寸中的对齐方式"}
                ),
            },
            "optional": {
                "node_id": ("STRING", {"default": "", "tooltip": "自定义节点ID"}),
                "mask": ("MASK",),  # 可选局部遮罩输入，用于裁剪图像
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "process"
    CATEGORY = "danbooru"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return time.time()

    def __init__(self):
        self.temp_dir = Path("/mnt/d/open_in_krita")
        self.temp_dir.mkdir(exist_ok=True)
        self.installer = KritaPluginInstaller()
        # 写死Alpha处理方式：保留透明
        self.alpha_handling = "无填充（保留透明）"

    # ---------------------- 新增：计算对齐偏移量 ----------------------
    def _get_alignment_offset(self, target_w: int, target_h: int, scaled_w: int, scaled_h: int, alignment: str) -> Tuple[int, int]:
        """根据对齐方式计算图像在目标尺寸中的偏移量"""
        if alignment == "居中对齐":
            x = (target_w - scaled_w) // 2
            y = (target_h - scaled_h) // 2
        elif alignment == "左上对齐":
            x = 0
            y = 0
        elif alignment == "右上对齐":
            x = target_w - scaled_w
            y = 0
        elif alignment == "左下对齐":
            x = 0
            y = target_h - scaled_h
        elif alignment == "右下对齐":
            x = target_w - scaled_w
            y = target_h - scaled_h
        else:  # 默认居中
            x = (target_w - scaled_w) // 2
            y = (target_h - scaled_h) // 2
        return x, y

    # ---------------------- 核心：用遮罩裁剪图像 ----------------------
    def _crop_image_with_mask(self, pil_image: Image.Image, mask: torch.Tensor) -> Image.Image:
        """用遮罩裁剪图像，仅保留被选中的白色区域，其余部分设为透明"""
        try:
            # 处理遮罩维度（[B, H, W] → [H, W]）
            if mask.dim() == 3:
                mask = mask[0]
            # 遮罩张量转PIL灰度图（0→黑色，1→白色）
            np_mask = (mask.cpu().numpy() * 255).astype(np.uint8)
            pil_mask = Image.fromarray(np_mask, mode="L")

            # 缩放遮罩到图像尺寸（保持一致）
            pil_mask = pil_mask.resize(pil_image.size, Image.LANCZOS)
            np_mask_scaled = np.array(pil_mask)

            # 二值化遮罩（仅纯白=选中，纯黑=未选中）
            np_mask_binary = (np_mask_scaled > 127).astype(np.uint8) * 255

            # 图像转numpy数组（保留Alpha通道）
            np_image = np.array(pil_image)
            if np_image.shape[-1] != 4:
                # 非RGBA格式添加Alpha通道
                np_image = np.dstack([np_image, np.ones_like(np_image[..., :1]) * 255])

            # 用遮罩过滤图像：未选中区域Alpha设为0（透明）
            np_image[np_mask_binary == 0, 3] = 0  # Alpha通道置0

            # 转回PIL图像
            return Image.fromarray(np_image, mode="RGBA")
        except Exception as e:
            logger.error(f"遮罩裁剪图像失败：{e}")
            return pil_image  # 失败时返回原图像

    # ---------------------- 简化后的Alpha处理逻辑 ----------------------
    def _handle_alpha_channel(self, pil_image: Image.Image) -> Image.Image:
        """强制保留透明，无需用户选择"""
        if pil_image.mode != "RGBA":
            return pil_image.convert("RGBA")  # 统一转为RGBA格式
        return pil_image

    # ---------------------- 原有复用逻辑（调整Alpha相关） ----------------------
    def _convert_linux_to_windows_path(self, linux_path: Path) -> str:
        linux_path_str = str(linux_path.resolve())
        for linux_prefix, win_prefix in LINUX_TO_WINDOWS_PATH_MAP.items():
            if linux_path_str.startswith(linux_prefix):
                return linux_path_str.replace(linux_prefix, win_prefix).replace("/", "\\")
        return linux_path_str

    # ---------------------- 保存局部遮罩到共享目录（不变） ----------------------
    def _save_mask_to_shared_dir(self, mask: torch.Tensor, unique_id: str) -> Optional[Path]:
        """将ComfyUI MASK转为Krita可识别的局部选区蒙版（PNG灰度图）"""
        if mask is None:
            return None
        try:
            if mask.dim() == 3:
                mask = mask[0]
            np_mask = (mask.cpu().numpy() * 255).astype(np.uint8)
            pil_mask = Image.fromarray(np_mask, mode="L")

            timestamp = int(time.time() * 1000)
            mask_filename = f"comfyui_mask_{unique_id}_{timestamp}.png"
            mask_path = self.temp_dir / mask_filename

            pil_mask.save(str(mask_path), format="PNG")
            logger.info(f"局部遮罩已保存到共享目录：{mask_path}")
            return mask_path
        except Exception as e:
            logger.error(f"保存遮罩失败：{e}")
            return None

    # ---------------------- 核心：请求创建逻辑（新增对齐参数） ----------------------
    def _create_open_request(self, image_path: Path, mask_path: Optional[Path], 
                             layer_position: str, alignment: str, node_id: str, unique_id: str) -> bool:
        try:
            if not node_id:
                node_id = f"send_node_{unique_id}_{int(time.time())}"

            # 防抖检查
            current_time = time.time()
            image_key = str(image_path.resolve())
            if image_key in _sent_requests and (current_time - _sent_requests[image_key] < 5.0):
                logger.warning(f"5秒内重复请求，跳过：{image_path.name}")
                return True
            _sent_requests[image_key] = current_time

            # 生成请求文件（新增alignment参数）
            timestamp = int(time.time() * 1000)
            request_file = self.temp_dir / f"open_{node_id}_{timestamp}.request"

            request_data = {
                "image_path": self._convert_linux_to_windows_path(image_path),
                "mask_path": self._convert_linux_to_windows_path(mask_path) if mask_path else None,
                "layer_position": layer_position,
                "alignment": alignment,  # 传递对齐方式
                "node_id": node_id,
                "timestamp": timestamp,
                "auto_open": True
            }

            with open(request_file, "w", encoding="utf-8") as f:
                json.dump(request_data, f, ensure_ascii=False, indent=2)

            logger.info(f"已创建open请求（层级：{layer_position}，对齐：{alignment}，Alpha：{self.alpha_handling}）：{request_file.name}")
            return True
        except Exception as e:
            logger.error(f"创建open请求失败：{e}")
            return False

    # ---------------------- 主处理逻辑（新增对齐参数） ----------------------
    def process(self, image: torch.Tensor, active: bool, auto_open: bool, layer_position: str, alignment: str,
                node_id: str, unique_id: str, mask: Optional[torch.Tensor] = None) -> Tuple[torch.Tensor]:
        if not active:
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": "ℹ️ 节点已禁用，未发送图像",
                "type": "info"
            })
            return (image,)

        # 检查插件安装
        if not self.installer.check_plugin_installed():
            logger.warning("Krita插件未安装，开始自动安装")
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": "📦 Krita插件未安装，正在自动安装...",
                "type": "info"
            })
            success = self.installer.install_plugin()
            if not success:
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": f"⚠️ Krita插件安装失败\n{PLUGIN_ENABLE_HINT}",
                    "type": "error"
                })
                return (image,)
            else:
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": f"✓ Krita插件安装成功\n请重启Krita后再次发送",
                    "type": "success"
                })
                return (image,)

        # 1. 保存图像到共享目录（有遮罩则先裁剪，应用对齐）
        # 目标尺寸：新建文档用图像原始尺寸，当前文档用文档尺寸（这里先按图像尺寸，Krita端会适配）
        save_path = self._save_image_to_shared_dir(image, unique_id, mask, alignment)
        if not save_path:
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": "⚠️ 图像保存失败，未发送到Krita",
                "type": "error"
            })
            return (image,)

        # 2. 保存局部遮罩到共享目录（可选）
        mask_path = self._save_mask_to_shared_dir(mask, unique_id) if mask is not None else None

        # 3. 自动处理（仅2个核心层级）
        if auto_open:
            krita_running = self._is_krita_running()
            if not krita_running:
                mask_msg = "（已裁剪选中部分）" if mask_path else ""
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": f"ℹ️ 图像已保存，但Krita未运行{mask_msg}\n层级选择：{layer_position}\n对齐方式：{alignment}\nAlpha处理：{self.alpha_handling}\n共享目录：{save_path}",
                    "type": "warning"
                })
                return (image,)

            # 创建open请求（传递对齐参数）
            request_success = self._create_open_request(save_path, mask_path, layer_position, alignment, node_id, unique_id)
            if request_success:
                mask_msg = "（含局部遮罩，已裁剪选中部分）" if mask_path else ""
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": f"✓ 图像已发送到Krita{mask_msg}\n层级：{layer_position}\n对齐：{alignment}\nAlpha处理：{self.alpha_handling}",
                    "type": "success"
                })
            else:
                PromptServer.instance.send_sync("open-in-krita-notification", {
                    "node_id": unique_id,
                    "message": f"⚠️ 图像已保存，但通知Krita失败",
                    "type": "warning"
                })
        else:
            mask_msg = "（含局部遮罩，已裁剪选中部分）" if mask_path else ""
            PromptServer.instance.send_sync("open-in-krita-notification", {
                "node_id": unique_id,
                "message": f"✓ 图像已保存到共享目录 {mask_msg}\n层级选择：{layer_position}\n对齐方式：{alignment}\nAlpha处理：{self.alpha_handling}\n路径：{save_path}",
                "type": "info"
            })

        return (image,)

    # ---------------------- 辅助方法（新增对齐逻辑） ----------------------
    def _save_image_to_shared_dir(self, image: torch.Tensor, unique_id: str, mask: Optional[torch.Tensor] = None, alignment: str = "居中对齐") -> Optional[Path]:
        try:
            if image.dim() == 4:
                image = image[0]
            np_image = (image.cpu().numpy() * 255).astype(np.uint8)
            # 转为RGBA格式（保留透明）
            pil_image = Image.fromarray(np_image, mode="RGBA") if np_image.shape[-1] == 4 else Image.fromarray(np_image).convert("RGBA")
            
            # 有遮罩则裁剪图像
            if mask is not None:
                pil_image = self._crop_image_with_mask(pil_image, mask)
                logger.info(f"✓ 已用遮罩裁剪图像，仅保留选中部分")

            # 强制保留透明通道
            processed_image = self._handle_alpha_channel(pil_image)

            # 清理旧文件
            old_files = list(self.temp_dir.glob(f"comfyui_send_{unique_id}_*.png"))
            for old_file in old_files:
                try:
                    old_file.unlink()
                except Exception as e:
                    logger.warning(f"清理旧文件失败：{old_file.name} - {e}")

            # 保存处理后的图像（新建文档场景：直接保存，对齐在Krita端生效）
            timestamp = int(time.time() * 1000)
            filename = f"comfyui_send_{unique_id}_{timestamp}.png"
            save_path = self.temp_dir / filename
            processed_image.save(str(save_path), format="PNG", optimize=True)
            logger.info(f"图像已保存到共享目录：{save_path}（模式：{processed_image.mode}，对齐：{alignment}，Alpha处理：{self.alpha_handling}）")
            return save_path
        except Exception as e:
            logger.error(f"保存图像失败：{e}")
            return None

    def _is_krita_running(self) -> bool:
        try:
            plugin_loaded_flag = self.temp_dir / "_plugin_loaded.txt"
            if plugin_loaded_flag.exists():
                return True
            # 兜底检查
            temp_node_id = f"check_running_{int(time.time())}"
            timestamp = int(time.time() * 1000)
            request_file = self.temp_dir / f"check_document_{temp_node_id}_{timestamp}.request"
            response_file = self.temp_dir / f"check_document_{temp_node_id}_{timestamp}.response"
            with open(request_file, 'w', encoding='utf-8') as f:
                f.write(f"{temp_node_id}\n{timestamp}\n")
            max_wait = 5.0
            elapsed = 0
            while elapsed < max_wait:
                if response_file.exists():
                    break
                time.sleep(0.5)
                elapsed += 0.5
            if response_file.exists():
                with open(response_file, 'r', encoding='utf-8') as f:
                    response_data = json.load(f)
                request_file.unlink(missing_ok=True)
                response_file.unlink(missing_ok=True)
                return response_data.get("has_active_document", False)
            request_file.unlink(missing_ok=True)
            return False
        except Exception as e:
            logger.error(f"检测Krita运行状态失败：{e}")
            return False


# 节点注册（更新显示名称）
def get_node_class_mappings():
    return {
        "SendToKrita": SendToKrita
    }


def get_node_display_name_mappings():
    return {
        "SendToKrita": "发送图像到Krita (核心层级+强制透明+遮罩裁剪+对齐)"
    }


NODE_CLASS_MAPPINGS = get_node_class_mappings()
NODE_DISPLAY_NAME_MAPPINGS = get_node_display_name_mappings()
