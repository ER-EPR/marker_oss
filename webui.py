import os
import re
import tempfile
import requests
import json
import time
import streamlit as st
from PIL import Image

# ==========================================
# 匹配官方最新版的 API 路由
# ==========================================
API_BASE = "http://localhost:8000"
INFERENCE_URL = f"{API_BASE}/marker/inference"
RESULTS_URL = f"{API_BASE}/marker/results"

def markdown_insert_images(markdown, images_dict):
    """纯 Python 实现 Base64 图片插入"""
    if not images_dict:
        return markdown
    for image_name, img_data in images_dict.items():
        img_html = f'<img src="data:image/jpeg;base64,{img_data}" style="max-width:100%; margin-bottom: 10px;" />'
        pattern = r'!\[([^\]]*)\]\(' + re.escape(image_name) + r'\s*[^)]*\)'
        markdown = re.sub(pattern, img_html, markdown)
    return markdown

st.set_page_config(layout="wide", page_title="Marker V100 极速工作台")
col1, col2 = st.columns([0.5, 0.5])

st.markdown("""
# 🚀 Marker 高性能解析工作台 (纯 API 异步队列版)
基于官方最新异步微服务架构，支持快速上传并投递至 V100 队列。
""")

in_file = st.sidebar.file_uploader(
    "上传文档 (仅支持 PDF):",
    type=["pdf"],
)

if in_file is None:
    st.stop()

file_ext = in_file.name.split('.')[-1].lower()

# ==========================================
# 左侧：智能预览区
# ==========================================
with col1:
    st.subheader("原始文件预览")
    page_range = ""

    if file_ext == 'pdf':
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(in_file.getvalue())
            total_pages = len(pdf)
            page_number = st.number_input(f"预览页码 (共 {total_pages} 页):", min_value=0, value=0, max_value=max(0, total_pages - 1))

            page = pdf[page_number]
            pil_image = page.render(scale=2).to_pil()
            # 修复 Streamlit 弃用警告
            st.image(pil_image, width="stretch")

            page_range = st.sidebar.text_input("指定解析页码范围 (如: 0,5-10)", value="")
        except Exception as e:
            st.warning(f"PDF 预览加载失败: {e}")
            page_range = st.sidebar.text_input("指定解析页码范围 (如: 0,5-10)", value="")

# ==========================================
# 侧边栏：构造 config JSON
# ==========================================
output_format = st.sidebar.selectbox("输出格式", ["markdown", "json", "html", "chunks"], index=0)
use_llm = st.sidebar.checkbox("使用 LLM 增强", value=False)
force_ocr = st.sidebar.checkbox("强制 OCR (针对扫描件)", value=False)
strip_existing_ocr = st.sidebar.checkbox("剥离现有 OCR", value=False)
disable_ocr_math = st.sidebar.checkbox("禁用公式 OCR", value=False)

run_marker = st.sidebar.button("🚀 提交 V100 队列", type="primary", use_container_width=True)

if not run_marker:
    st.stop()

# ==========================================
# 右侧：异步队列提交与轮询
# ==========================================
with col2:
    st.subheader("任务状态与结果")

    config_dict = {
        "output_format": output_format,
        "use_llm": use_llm,
        "force_ocr": force_ocr,
        "strip_existing_ocr": strip_existing_ocr,
        "disable_ocr_math": disable_ocr_math,
    }
    if file_ext == 'pdf' and page_range:
        config_dict["page_range"] = page_range

    with st.status("🔗 正在连接 V100 推理集群...", expanded=True) as status:
        try:
            # 步骤 1: 提交推断任务
            status.update(label="📥 正在上传文件并投递至 RabbitMQ 队列...", state="running")
            files = {"file": (in_file.name, in_file.getvalue(), in_file.type)}
            data = {"config": json.dumps(config_dict)}

            submit_resp = requests.post(INFERENCE_URL, files=files, data=data, timeout=30)
            submit_resp.raise_for_status()

            submit_res_data = submit_resp.json()
            if isinstance(submit_res_data, dict):
                file_id = submit_res_data.get("file_id") or submit_res_data.get("id")
            else:
                file_id = str(submit_res_data).strip()

            if not file_id:
                status.update(label="❌ 任务投递失败：未获取到 file_id", state="error")
                st.stop()

            status.update(label=f"⏳ 任务已入队 (ID: {file_id})。等待 Worker 调度与处理...", state="running")

            # 步骤 2: 轮询查询结果
            result_data = None
            max_retries = 120

            for i in range(max_retries):
                poll_resp = requests.get(RESULTS_URL, params={"file_id": file_id, "download": True})
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()

                job_status = poll_data.get("status", "").lower() if isinstance(poll_data, dict) else ""

                # 【核心修复】：更鲁棒的完成状态判断与数据提取
                if job_status in ["pending", "processing", "queued", "running"]:
                    status.update(label=f"⚙️ V100 正在狂飙运算中... (已耗时 {i * 5} 秒)", state="running")
                    time.sleep(5)
                    continue
                elif job_status in ["failed", "error"]:
                    status.update(label=f"❌ 解析任务在服务端失败: {poll_data.get('error', '未知错误')}", state="error")
                    st.stop()
                elif job_status in ["complete", "completed", "done", "success"]:
                    # 任务明确完成，提取嵌套的 result 字段（如果没有 result 字段，则回退使用顶层数据）
                    result_data = poll_data.get("result") or poll_data.get("data") or poll_data
                    status.update(label="✅ 解析完成！渲染中...", state="complete")
                    break
                else:
                    # 兜底判断：如果没有明确的 status 字段，但数据字典里出现了确切的产物
                    if isinstance(poll_data, dict) and any(k in poll_data for k in ["markdown", "html", "text", "result"]):
                        result_data = poll_data.get("result") or poll_data
                        status.update(label="✅ 解析完成！渲染中...", state="complete")
                        break
                    else:
                        time.sleep(5)

            if not result_data:
                status.update(label="⚠️ 轮询超时，或者后端返回了无法解析的格式。", state="error")
                st.write("📊 最终轮询收到的原始数据:", poll_data) # 暴露原始数据便于排错
                st.stop()

        except requests.exceptions.RequestException as e:
            status.update(label=f"❌ API 通信异常: {e}", state="error")
            st.stop()

# 步骤 3: 渲染最终结果 (仅在状态块完成后执行)
    if result_data is not None:
        # ==========================================
        # 【核心修复】增加数据类型的防御性判断
        # ==========================================
        if isinstance(result_data, str):
            # 情况 A：API 极度精简，直接扔回了纯文本字符串
            text = result_data
            images = {}
        elif isinstance(result_data, dict):
            # 情况 B：API 依然返回的是一个结构化字典
            text = result_data.get("markdown") or result_data.get("html") or result_data.get("text", "")

            # 如果没抓到 text，且用户本身请求的就是 json 或 chunks，则保留整个字典
            if not text and output_format in ["json", "chunks"]:
                text = result_data

            images = result_data.get("images", {})
        else:
            # 情况 C：未知的奇葩类型兜底
            text = str(result_data)
            images = {}

        # ==========================================
        # 界面渲染逻辑
        # ==========================================
        if output_format == "markdown":
            if isinstance(text, str) and text.strip():
                render_text = markdown_insert_images(text, images)
                tab1, tab2 = st.tabs(["👁️ 效果预览", "💻 Markdown 源码"])
                with tab1:
                    st.markdown(render_text, unsafe_allow_html=True)
                with tab2:
                    st.code(text, language="markdown")
            else:
                st.warning("转换成功，但返回的文本内容为空。")
                with st.expander("查看后端返回的完整 JSON"):
                    st.write(result_data)

        elif output_format in ["json", "chunks"]:
            st.json(text)

        elif output_format == "html":
            # 兼容 html 格式
            html_text = text if isinstance(text, str) else str(text)
            st.html(html_text)
            st.code(html_text, language="html")
