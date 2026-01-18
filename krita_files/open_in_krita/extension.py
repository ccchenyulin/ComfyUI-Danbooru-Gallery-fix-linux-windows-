"""
Extension module - Krita扩展主类
彻底移除View依赖，消除View.setCurrentNode警告
"""
import tempfile
import os
import sys
import time
from pathlib import Path
from krita import Extension, Krita, Document
from PyQt5.QtCore import QFileSystemWatcher, QTimer
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
    """Open In Krita扩展 - 处理与ComfyUI的交互（无View依赖）"""
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.comm = get_communication()
        self.logger = get_logger()

        self.watcher = None
        self.monitor_dir = Path("A:/D/open_in_krita")
        self.monitor_dir.mkdir(exist_ok=True)
        self.processed_files = set()  # 跟踪已处理的文件，避免重复打开
        self.opened_documents = {}  # 映射：文件路径 -> 文档对象（用于fetch请求）
        self.processed_requests = set()  # 跟踪已处理的请求文件名，避免重复处理

        self.logger.info("扩展已初始化")
        self.logger.info(f"监控目录: {self.monitor_dir}")
        self.logger.info(f"日志文件: {self.logger.get_log_path()}")

    def setup(self):
        """设置扩展（当Krita启动时调用）"""
        self.logger.info("开始设置扩展...")
        # 清理所有旧的请求文件
        self._cleanup_old_request_files()
        # 启动目录监控
        self._setup_directory_watcher()
        self.logger.info("目录监控器已启动")
        # 监听Krita文档打开事件
        self._setup_document_listener()
        self.logger.info("文档打开监听器已启动")
        # 创建插件加载完成标志文件
        try:
            plugin_loaded_flag = self.monitor_dir / "_plugin_loaded.txt"
            with open(plugin_loaded_flag, 'w', encoding='utf-8') as f:
                f.write(f"Plugin loaded at: {time.time()}\n")
            self.logger.info(f"✓ 插件加载标志文件已创建: {plugin_loaded_flag.name}")
        except Exception as e:
            self.logger.error(f"✗ 创建插件加载标志文件失败: {e}")

    def _cleanup_old_request_files(self):
        """清理所有旧的请求文件（启动时调用）"""
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
        """设置文档打开监听器（用于命令行启动）"""
        try:
            app = Krita.instance()
            notifier = app.notifier()
            # 监听viewCreated事件（当打开文档时会创建视图）
            notifier.viewCreated.connect(self._on_view_created)
            self.logger.info("✓ 已连接viewCreated事件监听器")
        except Exception as e:
            self.logger.error(f"✗ 设置文档监听器失败: {e}")
            import traceback
            traceback.print_exc()

    def _on_view_created(self):
        """当新视图创建时触发（文档被打开）"""
        try:
            self.logger.info("===== 检测到视图创建事件 =====")
            # 延迟500ms后激活图层，确保文档完全加载
            QTimer.singleShot(500, self._auto_activate_layer)
        except Exception as e:
            self.logger.error(f"✗ 处理viewCreated事件失败: {e}")
            import traceback
            traceback.print_exc()

    def _auto_activate_layer(self):
        """自动激活背景图层（纯Document方式，无View调用）"""
        try:
            self.logger.info("===== 开始自动激活图层 =====")
            app = Krita.instance()
            doc = app.activeDocument()
            if not doc:
                self.logger.warning("⚠ 没有活动文档，跳过图层激活")
                return

            self.logger.info(f"当前文档: {doc.name()}")
            # 获取所有图层
            child_nodes = doc.rootNode().childNodes()
            if not child_nodes:
                self.logger.warning("⚠ 文档没有图层")
                return

            # 优先查找背景图层（名为"Background"或"背景"）
            target_node = None
            for node in child_nodes:
                node_name_lower = node.name().lower()
                if 'background' in node_name_lower or '背景' in node.name():
                    target_node = node
                    self.logger.info(f"✓✓ 找到背景图层: {node.name()}")
                    break

            # 没有背景图层则优先查找绘画图层
            if not target_node:
                for node in child_nodes:
                    if node.type() == "paintlayer":
                        target_node = node
                        self.logger.info(f"✓ 找到第一个绘画图层: {node.name()}")
                        break

            # 没有绘画图层则使用第一个节点
            if not target_node:
                target_node = child_nodes[0]
                self.logger.info(f"使用第一个节点: {target_node.name()} (类型: {target_node.type()})")

            # 核心：仅用Document激活图层（无View调用）
            self.logger.info(f"正在激活图层: {target_node.name()}")
            doc.setActiveNode(target_node)
            self.logger.info("✓ 已通过Document设置活动节点")

            # 激活工具（不依赖View）
            try:
                app.action('KritaShape/KisToolSelectRectangular').trigger()
                self.logger.info("✓ 已激活矩形选择工具")
            except:
                try:
                    app.action('KritaShape/KisToolBrush').trigger()
                    self.logger.info("✓ 已激活画笔工具")
                except:
                    self.logger.info("⚠ 工具激活失败（非关键）")

            # 刷新文档确保生效
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
            self.logger.info(f"窗口是否最小化: {bool(is_minimized)}")
            if is_minimized:
                SW_RESTORE = 9
                self.logger.info("正在恢复最小化窗口...")
                ShowWindow(hwnd, SW_RESTORE)
                self.logger.info("✓ 窗口已恢复")
                time.sleep(0.1)

            self.logger.info(f"正在调用SetForegroundWindow({hwnd})...")
            result = SetForegroundWindow(hwnd)
            self.logger.info(f"SetForegroundWindow返回值: {result}")

            time.sleep(0.05)
            new_foreground = GetForegroundWindow()
            self.logger.info(f"激活后前台窗口句柄: {new_foreground}")
            if new_foreground == hwnd:
                self.logger.info("✓✓✓ Krita窗口已成功激活（验证通过）")
                return True
            else:
                self.logger.warning(f"✗ 激活可能失败：预期前台窗口={hwnd}，实际前台窗口={new_foreground}")
                return False
        except Exception as e:
            self.logger.error(f"✗✗✗ 激活窗口时出错: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _setup_layers(self, doc):
        """设置文档图层：使所有图层可见并激活第一个图层（无View依赖）"""
        self.logger.info("===== 开始设置图层 =====")
        try:
            if not doc:
                self.logger.error("✗ 文档对象为空")
                return False

            self.logger.info(f"文档名称: {doc.name()}")
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
            if not child_nodes or len(child_nodes) == 0:
                self.logger.warning("✗ 文档没有图层")
                return False

            self.logger.info(f"文档共有 {len(child_nodes)} 个图层:")
            for i, node in enumerate(child_nodes):
                self.logger.info(f"  图层{i}: 名称='{node.name()}', 类型={node.type()}, 可见={node.visible()}")

            # 步骤1：使所有图层可见
            self.logger.info("正在设置所有图层可见...")
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

            # 步骤2：激活目标图层（纯Document方式）
            target_node = None
            # 优先查找背景图层
            for node in child_nodes:
                node_name_lower = node.name().lower()
                if 'background' in node_name_lower or '背景' in node.name():
                    target_node = node
                    self.logger.info(f"✓✓ 找到背景图层: {node.name()}")
                    break

            # 没有背景图层则找绘画图层
            if not target_node:
                for node in child_nodes:
                    if node.type() == "paintlayer":
                        target_node = node
                        self.logger.info(f"✓ 找到第一个绘画图层: {node.name()}")
                        break

            # 没有绘画图层则用第一个节点
            if not target_node:
                target_node = child_nodes[0]
                self.logger.info(f"未找到特定图层，使用第一个节点: {target_node.name()}")

            # 核心：仅通过Document激活图层
            self.logger.info(f"正在激活图层: {target_node.name()}")
            doc.setActiveNode(target_node)
            self.logger.info("✓ 已通过Document设置活动节点")

            # 激活工具（不依赖View）
            try:
                app.action('KritaShape/KisToolSelectRectangular').trigger()
                self.logger.info("✓ 已激活矩形选择工具")
            except:
                try:
                    app.action('KritaShape/KisToolBrush').trigger()
                    self.logger.info("✓ 已激活画笔工具")
                except:
                    self.logger.info("⚠ 工具激活失败（非关键）")

            # 刷新文档确保生效
            doc.refreshProjection()
            doc.waitForDone()
            self.logger.info("✓ 文档已刷新")

            # 验证激活结果
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

    def _handle_fetch_request(self, request_file: Path):
        """处理fetch请求文件"""
        try:
            self.logger.info(f"===== 处理fetch请求: {request_file.name} =====")
            # 重命名请求文件为.processing
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

            # 解析文件名
            filename = processing_file.stem.replace('.processing', '')
            parts = filename.split('_')
            if len(parts) < 3:
                self.logger.error(f"✗ 请求文件名格式错误: {processing_file.name}")
                processing_file.unlink(missing_ok=True)
                return

            node_id = parts[1]
            timestamp = parts[2]
            self.logger.info(f"Node ID: {node_id}, Timestamp: {timestamp}")

            # 获取Krita数据
            self.logger.info("正在获取当前Krita数据...")
            image_path, mask_path = self.comm.get_current_krita_data()
            if not image_path:
                self.logger.error("✗ 获取Krita数据失败")
                processing_file.unlink(missing_ok=True)
                return

            # 创建响应文件
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

            # 清理文件
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
        """目录内容改变时的回调"""
        QTimer.singleShot(300, self._check_new_files)

    def _handle_check_document_request(self, request_file: Path):
        """处理check_document请求文件"""
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

            # 检查活动文档
            app = Krita.instance()
            active_doc = app.activeDocument()
            has_active_document = active_doc is not None
            self.logger.info(f"活动文档检查结果: {'有文档' if has_active_document else '无文档'}")

            # 创建响应文件
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

    def _handle_open_request(self, request_file: Path):
        """处理open请求文件"""
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

            # 读取请求内容
            import json
            with open(processing_file, 'r', encoding='utf-8') as f:
                request_data = json.load(f)
            image_path_str = request_data.get("image_path")
            node_id = request_data.get("node_id")

            if not image_path_str:
                self.logger.error("✗ 请求中缺少image_path")
                processing_file.unlink(missing_ok=True)
                return

            image_path = Path(image_path_str)
            if not image_path.exists():
                self.logger.error(f"✗ 图像文件不存在: {image_path}")
                processing_file.unlink(missing_ok=True)
                return

            self.logger.info(f"节点ID: {node_id}")
            self.logger.info(f"图像路径: {image_path}")

            # 检查是否已打开
            file_key = str(image_path.resolve())
            if file_key in self.opened_documents:
                existing_doc = self.opened_documents[file_key]
                if existing_doc and existing_doc.name():
                    self.logger.info(f"⚠ 图像已打开，跳过重复打开: {image_path.name}")
                    processing_file.unlink(missing_ok=True)
                    return

            # 打开图像
            app = Krita.instance()
            original_batchmode = app.batchmode()
            app.setBatchmode(True)
            self.logger.info("✓ 已启用批处理模式（打开文档）")
            try:
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

                    # 添加视图
                    window = app.activeWindow()
                    if not window:
                        self.logger.warning("⚠ activeWindow返回None，尝试使用windows()[0]")
                        windows_list = app.windows()
                        if windows_list:
                            window = windows_list[0]
                            self.logger.info(f"✓ 使用windows()[0]获取窗口")
                        else:
                            self.logger.error("✗ windows()列表为空，无法获取Krita窗口")

                    if window:
                        window.addView(doc)
                        self.logger.info(f"✓ 已打开: {image_path.name}")
                        # 存储文档映射
                        self.opened_documents[file_key] = doc
                        self.logger.info(f"✓ 已存储文档映射: {file_key}")
                        self.processed_files.add(image_path)

                        # 延迟设置图层
                        def delayed_setup():
                            try:
                                self.logger.info(f"===== 延迟设置开始: {doc.name()} =====")
                                if window:
                                    window.activate()
                                    self.logger.info("✓ 窗口已激活")
                                app.setActiveDocument(doc)
                                self.logger.info("✓ 文档已设置为活动")
                                self._setup_layers(doc)
                            except Exception as e:
                                self.logger.error(f"延迟设置图层失败: {e}")
                                import traceback
                                traceback.print_exc()
                        QTimer.singleShot(2000, delayed_setup)
                    else:
                        self.logger.error(f"✗ 无法获取Krita窗口，无法显示文档: {image_path.name}")
                else:
                    self.logger.error(f"✗ 打开失败: {image_path.name}")
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

            # 禁用PNG自动打开，仅通过open请求机制
            return
        except Exception as e:
            self.logger.error(f"检查新文件时出错: {e}")
            import traceback
            traceback.print_exc()

    def createActions(self, window):
        """创建菜单动作（空实现）"""
        pass
