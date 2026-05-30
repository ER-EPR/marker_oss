import os
import re
import tempfile
from typing import Any, Dict

import streamlit as st
from PIL import Image
import requests  # 新增：用于请求 API

# 保留轻量级的 CPU 辅助函数，用于左侧的 PDF 预览
from marker.scripts.common import (
    parse_args,
    img_to_html,
    get_page_image,
    page_count,
)

os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
os.environ["IN_STREAMLIT"] = "true"

from streamlit.runtime.uploaded_file_manager import UploadedFile

# ==========================================
# 配置你的远端 API 地址
# ==========================================
API_URL = "http://localhost:8000/convert"  # 根据你的实际后端地址修改


def markdown_insert_images(markdown, images):
    image_tags = re.findall(
        r'(!\[(?P<image_title>[^\]]*)\]\((?P<image_path>[^\)"\s]+)\s*([^\)]*)\))',
        markdown,
    )

    for image in image_tags:
        image_markdown = image[0]
        image_alt = image[1]
        image_path = image[2]
        if image_path in images:
            markdown = markdown.replace(
                image_markdown, img_to_html(images[image_path], image_alt)
            )
    return markdown


st.set_page_config(layout="wide")
col1, col2 = st.columns([0.5, 0.5])

# 【移除】去掉了耗费 V100 显存的 model_dict = load_models()
cli_options = parse_args()

st.markdown("""
# Marker API 驱动版工作台
这是一个改造版的官方 UI。推理请求将被发送至后端 API 执行，前端 Web 服务实现 **零显存占用**。
""")

in_file: UploadedFile = st.sidebar.file_uploader(
    "PDF, document, or image file:",
    type=["pdf", "png", "jpg", "jpeg", "gif", "pptx", "docx", "xlsx", "html", "epub"],
)

if in_file is None:
    st.stop()

filetype = in_file.type

# 左侧区域：本地 CPU 处理 PDF 预览，不耗费显存
with col1:
    total_pages = page_count(in_file)
    page_number = st.number_input(
        f"Page number out of {total_pages}:", min_value=0, value=0, max_value=total_pages
    )
    pil_image = get_page_image(in_file, page_number)
    st.image(pil_image, use_container_width=True)

# 侧边栏参数收集
page_range = st.sidebar.text_input(
    "Page range to parse, comma separated like 0,5-10,20",
    value=f"{page_number}-{page_number}",
)
output_format = st.sidebar.selectbox(
    "Output format", ["markdown", "json", "html", "chunks"], index=0
)
run_marker = st.sidebar.button("Run API 转换", type="primary")

use_llm = st.sidebar.checkbox("Use LLM", help="Use LLM for higher quality processing", value=False)
force_ocr = st.sidebar.checkbox("Force OCR", help="Force OCR on all pages", value=False)
strip_existing_ocr = st.sidebar.checkbox("Strip existing OCR", help="Strip existing OCR text from the PDF and re-OCR.", value=False)
debug = st.sidebar.checkbox("Debug", help="Show debug information", value=False)
disable_ocr_math = st.sidebar.checkbox("Disable math", help="Disable math in OCR output - no inline math", value=False)

if not run_marker:
    st.stop()

# ==========================================
# 执行转换：由本地模型推理改为 API 调用
# ==========================================
with tempfile.TemporaryDirectory() as tmp_dir:
    temp_pdf = os.path.join(tmp_dir, "temp.pdf")
    with open(temp_pdf, "wb") as f:
        f.write(in_file.getvalue())

    # 将用户的选项打包，作为请求参数发给 API
    api_payload = {
        "output_format": output_format,
        "page_range": page_range,
        "force_ocr": str(force_ocr).lower(),
        "use_llm": str(use_llm).lower(),
        "strip_existing_ocr": str(strip_existing_ocr).lower(),
        "disable_ocr_math": str(disable_ocr_math).lower(),
    }

    with st.spinner(f"正在向 {API_URL} 发送推理请求，请稍候..."):
        try:
            with open(temp_pdf, "rb") as f:
                # 假设远端 API 接受 multipart/form-data 格式的文件上传
                files = {"file": (in_file.name, f, filetype)}
                response = requests.post(API_URL, files=files, data=api_payload)
            
            response.raise_for_status()
            api_result = response.json()
            
            # 从 API 返回的 JSON 中提取文本和图片（兼容不同的 output_format）
            text = api_result.get("markdown") or api_result.get("html") or api_result.get("text", "")
            if output_format in ["json", "chunks"]:
                text = api_result
            
            images = api_result.get("images", {})
            metadata = api_result.get("metadata", {})
            
        except requests.exceptions.RequestException as e:
            st.error(f"API 请求失败。请检查后端容器是否正常运行。\n错误信息: {e}")
            st.stop()

# 右侧区域：渲染 API 返回的解析结果
with col2:
    if output_format == "markdown":
        # 兼容文本非空的情况
        if isinstance(text, str):
            text = markdown_insert_images(text, images)
            st.markdown(text, unsafe_allow_html=True)
        else:
            st.write(text)
    elif output_format in ["json", "chunks"]:
        st.json(text)
    elif output_format == "html":
        st.html(text)

if debug:
    with col1:
        st.write("📊 API 原始返回数据:")
        # 在 debug 模式下直接展示 API 返回的字典内容（排除了长文本避免卡顿）
        debug_output = {k: v for k, v in api_result.items() if k not in ['markdown', 'html']}
        st.json(debug_output)
        
        st.write("📝 解析结果源码:")
        st.code(text if isinstance(text, str) else str(text), language=output_format if output_format != "chunks" else "json")
