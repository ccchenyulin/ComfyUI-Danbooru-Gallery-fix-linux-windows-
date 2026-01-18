"""
Open In Krita - ComfyUI节点模块
包含：发送图像到Krita + 从Krita获取数据 双节点
"""

# 导入两个节点的映射（新增SendToKrita导入）
from .open_in_krita import NODE_CLASS_MAPPINGS as fetch_mappings, NODE_DISPLAY_NAME_MAPPINGS as fetch_display_mappings
from .send_to_krita import NODE_CLASS_MAPPINGS as send_mappings, NODE_DISPLAY_NAME_MAPPINGS as send_display_mappings

# 合并两个节点的映射（关键步骤）
NODE_CLASS_MAPPINGS = {**fetch_mappings, **send_mappings}
NODE_DISPLAY_NAME_MAPPINGS = {**fetch_display_mappings, **send_display_mappings}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']
