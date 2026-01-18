"""
Extension module - Krita扩展主类
完美兼容版：适配Krita 5.2.14+等比例缩放+对齐方式
仅保留2个核心层级，支持5种图像对齐方式
"""
import tempfile
import os
import sys
import time
from pathlib import Path
from typing import Tuple  # 新增：导入Tuple类型注解
from krita import Extension, Krita, Document, Selection
from PyQt5.QtCore import QFileSystemWatcher, QTimer, Qt, QByteArray
from PyQt5.QtGui import QImage, QColor, QPainter
from .communication import get_communication
from .logger import get_logger

# Windows窗口激活支持
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes
        HAS_WIN32 = True
    except ImportError:
        HAS_WIN32 = False
else:
    HAS_WIN32 = False

class OpenInKritaExtension(Extension):
    """Open In Krita扩展 - 仅核心层级+等比例缩放+对齐方式支持"""
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.comm = get_communication()
        self.logger = get_logger()

        self.watcher = None
        self.monitor_dir = Path("A:/D/open_in_krita")
        self.monitor_dir.mkdir(exist_ok=True)
        self.processed_files = set()
        self.opened_documents = {}
        self.processed_requests = set()

        self.logger.info("扩展已初始化")
        self.logger.info(f"监控目录: {self.monitor_dir}")
        self.logger.info(f"日志文件: {self.logger.get_log_path()}")

    def setup(self):
        """设置扩展"""
        self.logger.info("开始设置扩展...")
        self._cleanup_old_request_files()
        self._setup_directory_watcher()
        self.logger.info("目录监控器已启动")
        self._setup_document_listener()
        self.logger.info("文档打开监听器已启动")
        try:
            plugin_loaded_flag = self.monitor_dir / "_plugin_loaded.txt"
            with open(plugin_loaded_flag, 'w', encoding='utf-8') as f:
                f.write(f"Plugin loaded at: {time.time()}\n")
            self.logger.info(f"✓ 插件加载标志文件已创建: {plugin_loaded_flag.name}")
        except Exception as e:
            self.logger.error(f"✗ 创建插件加载标志文件失败: {e}")

    def _cleanup_old_request_files(self):
        """清理旧请求文件"""
        try:
            self.logger.info("===== 清理旧请求文件 =====")
            request_patterns = ["open_*.request", "fetch_*.request", "check_document_*.request"]
            total_cleaned = 0
            for pattern in request_patterns:
                files = list(self.monitor_dir.glob(pattern))
                for f in files:
                    try:
                        f.unlink()
                        total_cleaned += 1
                        self.logger.info(f"✓ 已删除旧请求: {f.name}")
                    except Exception as e:
                        self.logger.warning(f"⚠ 删除失败: {f.name} - {e}")
            if total_cleaned > 0:
                self.logger.info(f"✓ 共清理 {total_cleaned} 个旧请求文件")
            else:
                self.logger.info("无需清理（没有旧请求文件）")
            self.logger.info("===== 清理完成 =====")
        except Exception as e:
            self.logger.error(f"✗ 清理旧请求文件时出错: {e}")
            import traceback
            traceback.print_exc()

    def _setup_directory_watcher(self):
        """设置目录监控"""
        if self.watcher is None:
            self.watcher = QFileSystemWatcher()
            self.watcher.addPath(str(self.monitor_dir))
            self.watcher.directoryChanged.connect(self._on_directory_changed)
            self.logger.info(f"正在监控目录: {self.monitor_dir}")

    def _setup_document_listener(self):
        """设置文档监听器"""
        try:
            app = Krita.instance()
            notifier = app.notifier()
            notifier.viewCreated.connect(self._on_view_created)
            self.logger.info("✓ 已连接viewCreated事件监听器")
        except Exception as e:
            self.logger.error(f"✗ 设置文档监听器失败: {e}")
            import traceback
            traceback.print_exc()

    def _on_view_created(self):
        """视图创建事件"""
        try:
            self.logger.info("===== 检测到视图创建事件 =====")
            QTimer.singleShot(500, self._auto_activate_layer)
        except Exception as e:
            self.logger.error(f"✗ 处理viewCreated事件失败: {e}")
            import traceback
            traceback.print_exc()

    def _auto_activate_layer(self):
        """自动激活背景图层"""
        try:
            self.logger.info("===== 开始自动激活图层 =====")
            app = Krita.instance()
            doc = app.activeDocument()
            if not doc:
                self.logger.warning("⚠ 没有活动文档，跳过图层激活")
                return

            self.logger.info(f"当前文档: {doc.name() if doc.name() else '未命名'}")
            child_nodes = doc.rootNode().childNodes()
            if not child_nodes:
                self.logger.warning("⚠ 文档没有图层")
                return

            # 查找背景图层
            target_node = None
            for node in child_nodes:
                node_name_lower = node.name().lower()
                if 'background' in node_name_lower or '背景' in node.name():
                    target_node = node
                    self.logger.info(f"✓✓ 找到背景图层: {node.name()}")
                    break

            # 查找绘画图层
            if not target_node:
                for node in child_nodes:
                    if node.type() == "paintlayer":
                        target_node = node
                        self.logger.info(f"✓ 找到第一个绘画图层: {node.name()}")
                        break

            # 使用第一个节点
            if not target_node:
                target_node = child_nodes[0]
                self.logger.info(f"使用第一个节点: {target_node.name()} (类型: {target_node.type()})")

            # 激活图层
            doc.setActiveNode(target_node)
            self.logger.info("✓ 已通过Document设置活动节点")

            # 激活工具
            try:
                app.action('KritaShape/KisToolSelectRectangular').trigger()
                self.logger.info("✓ 已激活矩形选择工具")
            except:
                try:
                    app.action('KritaShape/KisToolBrush').trigger()
                    self.logger.info("✓ 已激活画笔工具")
                except:
                    self.logger.info("⚠ 工具激活失败（非关键）")

            doc.refreshProjection()
            doc.waitForDone()
            self.logger.info("✓ 文档已刷新，图层激活完成")
            self.logger.info("===== 图层激活完成 =====")
        except Exception as e:
            self.logger.error(f"✗ 自动激活图层失败: {e}")
            import traceback
            traceback.print_exc()

    def _activate_krita_window(self):
        """激活Krita窗口（Windows）"""
        self.logger.info("===== 开始激活Krita窗口 =====")
        if not HAS_WIN32:
            self.logger.warning("窗口激活功能仅支持Windows平台")
            return False
        try:
            FindWindow = ctypes.windll.user32.FindWindowW
            SetForegroundWindow = ctypes.windll.user32.SetForegroundWindow
            ShowWindow = ctypes.windll.user32.ShowWindow
            IsIconic = ctypes.windll.user32.IsIconic
            GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow()

            current_foreground = GetForegroundWindow
            self.logger.info(f"当前前台窗口句柄: {current_foreground}")

            window_classes = ["Qt5QWindowIcon", "Qt5152QWindowIcon", None]
            self.logger.info(f"将尝试以下窗口类名: {window_classes}")
            hwnd = None
            for wclass in window_classes:
                self.logger.info(f"尝试查找窗口类: {wclass if wclass else '任意类名'}")
                if wclass:
                    hwnd = FindWindow(wclass, None)
                    if hwnd and hwnd != 0:
                        self.logger.info(f"✓ 通过类名 '{wclass}' 找到窗口: {hwnd}")
                    else:
                        self.logger.info(f"× 类名 '{wclass}' 未找到窗口")
                else:
                    FindWindowEx = ctypes.windll.user32.FindWindowExW
                    hwnd = FindWindowEx(None, None, None, "Krita")
                    if hwnd and hwnd != 0:
                        self.logger.info(f"✓ 通过标题找到窗口: {hwnd}")
                    else:
                        self.logger.info("× 通过标题未找到窗口")
                if hwnd and hwnd != 0:
                    break

            if not hwnd or hwnd == 0:
                self.logger.warning("✗ 未找到Krita窗口句柄")
                return False

            self.logger.info(f"✓ 最终找到Krita窗口句柄: {hwnd}")
            is_minimized = IsIconic(hwnd)
            if is_minimized:
                SW_RESTORE = 9
                ShowWindow(hwnd, SW_RESTORE)
                self.logger.info("✓ 窗口已恢复")
                time.sleep(0.1)

            result = SetForegroundWindow(hwnd)
            self.logger.info(f"SetForegroundWindow返回值: {result}")

            time.sleep(0.05)
            new_foreground = GetForegroundWindow()
            if new_foreground == hwnd:
                self.logger.info("✓✓✓ Krita窗口已成功激活")
                return True
            else:
                self.logger.warning(f"✗ 激活可能失败：预期={hwnd}，实际={new_foreground}")
                return False
        except Exception as e:
            self.logger.error(f"✗✗✗ 激活窗口时出错: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _setup_layers(self, doc):
        """设置文档图层"""
        self.logger.info("===== 开始设置图层 =====")
        try:
            if not doc:
                self.logger.error("✗ 文档对象为空")
                return False

            self.logger.info(f"文档名称: {doc.name() if doc.name() else '未命名'}")
            self.logger.info(f"文档路径: {doc.fileName()}")
            self.logger.info(f"文档已修改: {doc.modified()}")

            current_active = doc.activeNode()
            if current_active:
                self.logger.info(f"当前活动图层: {current_active.name()} (类型: {current_active.type()})")
            else:
                self.logger.warning("当前没有活动图层")

            root_node = doc.rootNode()
            if not root_node:
                self.logger.warning("✗ 无法获取根节点")
                return False

            child_nodes = root_node.childNodes()
            if not child_nodes:
                self.logger.warning("✗ 文档没有图层")
                return False

            self.logger.info(f"文档共有 {len(child_nodes)} 个图层:")
            for i, node in enumerate(child_nodes):
                self.logger.info(f"  图层{i}: 名称='{node.name()}', 类型={node.type()}, 可见={node.visible()}")

            # 显示所有图层
            visible_count = 0
            for node in child_nodes:
                if not node.visible():
                    node.setVisible(True)
                    visible_count += 1
                    self.logger.info(f"  ✓ 已显示图层: {node.name()}")
            if visible_count > 0:
                self.logger.info(f"✓ 已显示 {visible_count} 个图层")
            else:
                self.logger.info("所有图层已可见，无需修改")

            # 激活目标图层
            target_node = None
            for node in child_nodes:
                node_name_lower = node.name().lower()
                if 'background' in node_name_lower or '背景' in node.name():
                    target_node = node
                    self.logger.info(f"✓✓ 找到背景图层: {node.name()}")
                    break

            if not target_node:
                for node in child_nodes:
                    if node.type() == "paintlayer":
                        target_node = node
                        self.logger.info(f"✓ 找到第一个绘画图层: {node.name()}")
                        break

            if not target_node:
                target_node = child_nodes[0]
                self.logger.info(f"未找到特定图层，使用第一个节点: {target_node.name()}")

            doc.setActiveNode(target_node)
            self.logger.info("✓ 已通过Document设置活动节点")

            # 激活工具
            try:
                app.action('KritaShape/KisToolSelectRectangular').trigger()
                self.logger.info("✓ 已激活矩形选择工具")
            except:
                try:
                    app.action('KritaShape/KisToolBrush').trigger()
                    self.logger.info("✓ 已激活画笔工具")
                except:
                    self.logger.info("⚠ 工具激活失败（非关键）")

            doc.refreshProjection()
            doc.waitForDone()
            self.logger.info("✓ 文档已刷新")

            time.sleep(0.1)
            new_active = doc.activeNode()
            if new_active:
                self.logger.info(f"✓✓✓ 图层设置成功 - 活动图层: {new_active.name()}")
                return True
            else:
                self.logger.warning("⚠ 无法验证活动图层，但设置可能已生效")
                return True
        except Exception as e:
            self.logger.error(f"✗✗✗ 设置图层时出错: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ---------------------- 新增：计算对齐偏移量（与节点端逻辑一致） ----------------------
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

    # ---------------------- 新增：应用对齐方式绘制图像 ----------------------
    def _draw_image_with_alignment(self, target_w: int, target_h: int, qimage_scaled: QImage, alignment: str) -> QImage:
        """创建目标尺寸的图像，按对齐方式粘贴缩放后的图像"""
        # 创建空白RGBA图像（保留透明）
        target_image = QImage(target_w, target_h, QImage.Format_ARGB32)
        target_image.fill(Qt.transparent)  # 填充透明背景

        # 计算偏移量
        x, y = self._get_alignment_offset(target_w, target_h, qimage_scaled.width(), qimage_scaled.height(), alignment)

        # 绘制缩放后的图像到目标位置
        painter = QPainter(target_image)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.drawImage(x, y, qimage_scaled)
        painter.end()

        return target_image

    def _handle_fetch_request(self, request_file: Path):
        """处理fetch请求"""
        try:
            self.logger.info(f"===== 处理fetch请求: {request_file.name} =====")
            processing_file = request_file.with_suffix('.processing')
            try:
                request_file.rename(processing_file)
                self.logger.info(f"✓ 请求文件已标记为处理中")
            except FileNotFoundError:
                self.logger.info(f"⚠ 请求文件已被处理，跳过")
                return
            except Exception as e:
                self.logger.warning(f"⚠ 重命名请求文件失败: {e}，继续处理")
                processing_file = request_file

            filename = processing_file.stem.replace('.processing', '')
            parts = filename.split('_')
            if len(parts) < 3:
                self.logger.error(f"✗ 请求文件名格式错误: {processing_file.name}")
                processing_file.unlink(missing_ok=True)
                return

            node_id = parts[1]
            timestamp = parts[2]
            self.logger.info(f"Node ID: {node_id}, Timestamp: {timestamp}")

            image_path, mask_path = self.comm.get_current_krita_data()
            if not image_path:
                self.logger.error("✗ 获取Krita数据失败")
                processing_file.unlink(missing_ok=True)
                return

            response_file = self.monitor_dir / f"fetch_{node_id}_{timestamp}.response"
            self.logger.info(f"创建响应文件: {response_file.name}")
            import json
            response_data = {
                "status": "success",
                "image_path": str(image_path) if image_path else None,
                "mask_path": str(mask_path) if mask_path else None
            }
            with open(response_file, 'w', encoding='utf-8') as f:
                json.dump(response_data, f, ensure_ascii=False, indent=2)

            self.logger.info(f"✓ 响应文件已创建: {response_file.name}")
            self.logger.info(f"  图像路径: {response_data['image_path']}")
            self.logger.info(f"  蒙版路径: {response_data['mask_path']}")

            processing_file.unlink(missing_ok=True)
            self.logger.info(f"✓ 请求文件已删除")
            self.logger.info(f"===== fetch请求处理完成 =====")
        except Exception as e:
            self.logger.error(f"✗ 处理fetch请求时出错: {e}")
            import traceback
            traceback.print_exc()
            try:
                processing_file.unlink(missing_ok=True)
            except:
                pass

    def _on_directory_changed(self, path):
        """目录变化回调"""
        QTimer.singleShot(300, self._check_new_files)

    def _handle_check_document_request(self, request_file: Path):
        """处理check_document请求"""
        try:
            self.logger.info(f"===== 处理check_document请求: {request_file.name} =====")
            filename = request_file.stem
            parts = filename.split('_')
            if len(parts) < 4:
                self.logger.error(f"✗ 请求文件名格式错误: {request_file.name}")
                request_file.unlink(missing_ok=True)
                return

            node_id = parts[2]
            timestamp = parts[3]
            self.logger.info(f"Node ID: {node_id}, Timestamp: {timestamp}")

            app = Krita.instance()
            active_doc = app.activeDocument()
            has_active_document = active_doc is not None
            self.logger.info(f"活动文档检查结果: {'有文档' if has_active_document else '无文档'}")

            response_file = self.monitor_dir / f"check_document_{node_id}_{timestamp}.response"
            self.logger.info(f"创建响应文件: {response_file.name}")
            import json
            response_data = {
                "has_active_document": has_active_document
            }
            with open(response_file, 'w', encoding='utf-8') as f:
                json.dump(response_data, f, ensure_ascii=False, indent=2)

            self.logger.info(f"✓ 响应文件已创建: {response_file.name}")
            request_file.unlink(missing_ok=True)
            self.logger.info(f"✓ 请求文件已删除")
            self.logger.info(f"===== check_document请求处理完成 =====")
        except Exception as e:
            self.logger.error(f"✗ 处理check_document请求时出错: {e}")
            import traceback
            traceback.print_exc()
            try:
                request_file.unlink(missing_ok=True)
            except:
                pass

    # 核心逻辑：仅保留2个核心层级+等比例缩放+对齐方式支持
    def _handle_open_request(self, request_file: Path):
        """处理open请求（支持对齐方式）"""
        try:
            request_name = request_file.name
            if request_name in self.processed_requests:
                self.logger.info(f"⚠ 请求已处理过，跳过: {request_name}")
                return

            self.processed_requests.add(request_name)
            self.logger.info(f"===== 处理open请求: {request_name} =====")

            # 重命名请求文件
            processing_file = request_file.with_suffix('.processing')
            try:
                request_file.rename(processing_file)
                self.logger.info(f"✓ 请求文件已标记为处理中")
            except FileNotFoundError:
                self.logger.info(f"⚠ 请求文件已被处理，跳过")
                return
            except Exception as e:
                self.logger.warning(f"⚠ 重命名请求文件失败: {e}，继续处理")
                processing_file = request_file

            # 读取请求内容（新增alignment参数）
            import json
            with open(processing_file, 'r', encoding='utf-8') as f:
                request_data = json.load(f)
            image_path_str = request_data.get("image_path")
            layer_position = request_data.get("layer_position", "新建独立文档")
            alignment = request_data.get("alignment", "居中对齐")  # 读取对齐方式，默认居中
            node_id = request_data.get("node_id")

            if not image_path_str:
                self.logger.error("✗ 请求中缺少image_path")
                processing_file.unlink(missing_ok=True)
                return

            # 图像文件重试逻辑
            image_path = Path(image_path_str)
            retry_count = 0
            max_retry = 3
            while retry_count < max_retry and not image_path.exists():
                self.logger.warning(f"⚠ 图像文件暂未找到，重试中（{retry_count+1}/{max_retry}）")
                time.sleep(0.5)
                retry_count += 1

            if not image_path.exists():
                self.logger.error(f"✗ 图像文件不存在: {image_path}")
                processing_file.unlink(missing_ok=True)
                return

            self.logger.info(f"节点ID: {node_id}")
            self.logger.info(f"图像路径: {image_path}")
            self.logger.info(f"层级选择: {layer_position}")
            self.logger.info(f"对齐方式: {alignment}")

            # 核心：层级处理（仅保留2个核心层级）
            app = Krita.instance()
            active_doc = app.activeDocument()
            original_batchmode = app.batchmode()
            app.setBatchmode(True)
            self.logger.info("✓ 已启用批处理模式")

            try:
                # 场景1：新建独立文档（按图像原始尺寸，应用对齐）
                if layer_position == "新建独立文档" or not active_doc:
                    doc = app.openDocument(str(image_path))
                    if doc:
                        # 删除自动保存文件
                        try:
                            autosave_file = Path(str(image_path) + "~")
                            if autosave_file.exists():
                                autosave_file.unlink()
                                self.logger.info(f"✓ 已删除自动保存文件: {autosave_file.name}")
                        except Exception as e:
                            self.logger.warning(f"⚠ 删除自动保存文件失败: {e}")

                        window = app.activeWindow() or (app.windows()[0] if app.windows() else None)
                        if window:
                            window.addView(doc)
                            self.logger.info(f"✓ 已新建文档打开: {image_path.name}")
                            self.opened_documents[str(image_path.resolve())] = doc
                            self.processed_files.add(image_path)

                            # 延迟设置（应用对齐）
                            def delayed_setup():
                                try:
                                    if window:
                                        window.activate()
                                    app.setActiveDocument(doc)
                                    self._setup_layers(doc)
                                    
                                    # 重新加载图像并应用对齐（确保对齐生效）
                                    qimage = QImage(str(image_path))
                                    if not qimage.isNull():
                                        target_w = doc.width()
                                        target_h = doc.height()
                                        # 等比例缩放
                                        qimage_scaled = qimage.scaled(
                                            target_w, target_h,
                                            Qt.KeepAspectRatio,
                                            Qt.SmoothTransformation
                                        )
                                        # 应用对齐方式
                                        aligned_image = self._draw_image_with_alignment(target_w, target_h, qimage_scaled, alignment)
                                        # 写入对齐后的图像数据
                                        active_node = doc.activeNode()
                                        pixel_data = aligned_image.bits().asstring(aligned_image.byteCount())
                                        active_node.setPixelData(pixel_data, 0, 0, target_w, target_h)
                                        doc.refreshProjection()
                                        self.logger.info(f"✓ 已应用{alignment}，图像尺寸：{target_w}x{target_h}")
                                except Exception as e:
                                    self.logger.error(f"延迟设置失败: {e}")
                            QTimer.singleShot(2000, delayed_setup)
                        else:
                            self.logger.error(f"✗ 无法获取Krita窗口")
                    else:
                        self.logger.error(f"✗ 新建文档失败: {image_path.name}")

                # 场景2：当前文档最上层（用文档尺寸，应用对齐）
                elif layer_position == "当前文档最上层":
                    self.logger.info(f"✓ 检测到活跃文档: {active_doc.name() if active_doc.name() else '未命名'}，插入最上层")
                    new_layer = active_doc.createNode("ComfyUI发送的图像", "paintlayer")
                    root_node = active_doc.rootNode()
                    
                    # 目标尺寸：文档尺寸
                    target_width = active_doc.width()
                    target_height = active_doc.height()
                    self.logger.info(f"✓ 目标尺寸: {target_width}x{target_height}（通过文档获取）")

                    # 插入到最上层
                    root_node.addChildNode(new_layer, None)

                    # 加载并按文档尺寸等比例缩放+对齐
                    qimage = QImage(str(image_path))
                    if not qimage.isNull():
                        # 等比例缩放（无拉伸/裁剪）
                        qimage_scaled = qimage.scaled(
                            target_width, target_height,
                            Qt.KeepAspectRatio,  # 保持原比例
                            Qt.SmoothTransformation  # 平滑缩放
                        )
                        self.logger.info(f"✓ 图像缩放完成：原始{qimage.width()}x{qimage.height()} → 缩放后{qimage_scaled.width()}x{qimage_scaled.height()}")

                        # 应用对齐方式
                        aligned_image = self._draw_image_with_alignment(target_width, target_height, qimage_scaled, alignment)
                        self.logger.info(f"✓ 已应用{alignment}，偏移量：{self._get_alignment_offset(target_width, target_height, qimage_scaled.width(), qimage_scaled.height(), alignment)}")

                        # 写入对齐后的图像数据
                        pixel_data = aligned_image.bits().asstring(aligned_image.byteCount())
                        new_layer.setPixelData(pixel_data, 0, 0, target_width, target_height)
                        active_doc.setActiveNode(new_layer)
                        active_doc.refreshProjection()
                        self.logger.info(f"✓ 图像已插入到当前文档最上层（{alignment}）")
                    else:
                        self.logger.error(f"✗ 加载图像到图层失败")

            finally:
                app.setBatchmode(original_batchmode)
                self.logger.info("✓ 已恢复批处理模式")

            # 清理文件
            processing_file.unlink(missing_ok=True)
            self.logger.info(f"✓ 请求文件已删除")
            self.logger.info(f"===== open请求处理完成 =====")
        except Exception as e:
            self.logger.error(f"✗ 处理open请求时出错: {e}")
            import traceback
            traceback.print_exc()
            try:
                processing_file.unlink(missing_ok=True)
            except:
                pass

    def _check_new_files(self):
        """检查新文件并处理请求"""
        try:
            # 处理check_document请求
            check_request_files = list(self.monitor_dir.glob("check_document_*.request"))
            for request_file in check_request_files:
                self.logger.info(f"检测到check_document请求: {request_file.name}")
                self._handle_check_document_request(request_file)

            # 处理fetch请求
            request_files = list(self.monitor_dir.glob("fetch_*.request"))
            for request_file in request_files:
                self.logger.info(f"检测到fetch请求: {request_file.name}")
                self._handle_fetch_request(request_file)

            # 处理open请求
            open_request_files = list(self.monitor_dir.glob("open_*.request"))
            for request_file in open_request_files:
                self.logger.info(f"检测到open请求: {request_file.name}")
                self._handle_open_request(request_file)

            return
        except Exception as e:
            self.logger.error(f"检查新文件时出错: {e}")
            import traceback
            traceback.print_exc()

    def createActions(self, window):
        """创建菜单动作（空实现）"""
        pass
