import asyncio
import datetime
import json
import logging
import mimetypes
import os

from pathlib import Path
import urllib
import uuid
import httpx
from fastapi import FastAPI, File, Form, Request, BackgroundTasks, HTTPException,Query,UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from typing import Dict, List
from utils import global_variables  # 需实现对应接口
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

app = FastAPI()
from fastapi.middleware.cors import CORSMiddleware


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 模板和静态文件配置
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 文件上传配置
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)  # 确保上传目录存在

# -------------------- 文件上传接口 --------------------
@app.post("/api/upload")
async def upload_files(
    session_id: str = Form(...),
    files: List[UploadFile] = File(...)
):
    """
    增强版文件上传接口：
    1. 自动创建会话目录
    2. 生成唯一文件名防止冲突
    3. 完整记录文件元数据
    4. 线程安全操作
    """
    if not files:
        raise HTTPException(status_code=400, detail="未上传任何文件")
    
    saved_files = []
    session_dir = os.path.join(UPLOAD_DIR, session_id)
    
    try:
        # 创建会话目录
        os.makedirs(session_dir, exist_ok=True)
        
        for file in files:
            # 安全检查：限制文件类型/大小
            if file.size > 50 * 1024 * 1024:  # 50MB限制
                raise HTTPException(status_code=400, detail=f"文件 {file.filename} 超过大小限制")
            
            # 生成唯一文件名
            file_ext = os.path.splitext(file.filename)[1]
            unique_filename = f"{uuid.uuid4().hex}{file_ext}"
            file_path = os.path.join(session_dir, unique_filename)
            
            # 保存文件
            with open(file_path, "wb") as buffer:
                content = await file.read()
                buffer.write(content)
            
            # 记录完整元数据
            file_info = {
                "original_name": file.filename,
                "saved_name": unique_filename,
                "file_path": file_path,
                "size": file.size,
                "content_type": file.content_type,
                "session_id": session_id
            }
            saved_files.append(file_info)
            logging.info(f"文件保存成功: {file.filename} -> {file_path}")
        
        # 异步保存到会话
        await global_variables.add_files_to_session(session_id, saved_files)
        
        return {
            "status": "success",
            "files": saved_files,
            "count": len(saved_files),
            "session_id": session_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("文件上传失败")
        raise HTTPException(status_code=500, detail=f"服务器处理错误: {str(e)}")

# -------------------- Agent 异步执行核心 --------------------
async def process_prompt_with_session(prompt: str, session_id: str, images: List[Dict] = None):
    from utils.global_variables import set_current_session, get_agent
    set_current_session(session_id)
    logging.info(f"Processing prompt for session {session_id}")

    try:
        agent = get_agent(session_id, images=images)
        await agent.run(prompt)
    except Exception as e:
        logging.error(f"Prompt error in session {session_id}: {str(e)}")

# -------------------- 任务后台执行包装 --------------------
def run_async_task(prompt: str, session_id: str, images: List[Dict] = None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(process_prompt_with_session(prompt, session_id, images))
    loop.close()

# -------------------- 接收用户回答入口 --------------------
@app.post("/api/answer")
async def answer_question(request: Request):
    data = await request.json()
    prompt = data.get("answer", "").strip()
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="message cannot be empty")
    try:
        print("answer_session_id:", session_id)
        q = global_variables.get_input_queue(session_id)
        await global_variables.clear_asyncio_queue(q)
        await q.put(prompt)
        return {"status": "received"}
    except Exception as e:
        logging.error(f"Error sending prompt to session {session_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- 接收用户请求入口 --------------------
@app.post("/api/send")
async def send_message(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    prompt = data.get("message", "").strip()
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="message cannot be empty")
    try:
        images = await global_variables.get_session_files(session_id)
        q = global_variables.get_stream_queue(session_id)
        await global_variables.clear_asyncio_queue(q)
        
        # 构建增强的prompt
        enhanced_prompt = build_enhanced_prompt(prompt, images)
        
        # 用后台任务异步运行，注入图片信息
        background_tasks.add_task(
            run_async_task, 
            prompt=enhanced_prompt,
            session_id=session_id,
            images=images
        )

        return {
            "status": "started",
            "stream_url": f"/api/stream/{session_id}",
            "stream_id": session_id,
            "session_id": session_id,
            "image_count": len(images)
        }
    except Exception as e:
        logging.error(f"Error starting prompt processing: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- SSE 流响应 --------------------
@app.get("/api/stream/{session_id}")
async def stream_response(session_id: str):
    q = global_variables.get_stream_queue(session_id)

    async def event_generator():
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=5.0)
                    if isinstance(chunk, dict):
                        if chunk.get("is_final"):
                            yield f"event: end\ndata: {json.dumps(chunk)}\n\n"
                            break
                        elif "error" in chunk:
                            yield f"event: error_event\ndata: {json.dumps(chunk)}\n\n"
                        else:
                            yield f"data: {json.dumps(chunk)}\n\n"
                    else:
                        yield f"data: {json.dumps({'text': str(chunk)})}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except Exception as e:
            yield f"event: error_event\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# -------------------- 取消/中止执行 --------------------
@app.post("/api/cancel")
async def cancel_stream(request: Request):
    data = await request.json()
    session_id = data.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    try:
        # 1. 关闭流队列
        global_variables.close_stream_queue(session_id)
        
        # 2. 关闭输入队列
        global_variables.close_input_queue(session_id)
        
        # 3. 释放Agent资源
        agent = global_variables.get_agent(session_id)
        if agent:
            if hasattr(agent, 'cleanup'):
                # 注意：这里需要同步调用，因为原版TwoStageAgent的cleanup是同步方法
                agent.cleanup()  
            global_variables.close_agent(session_id)
            logging.info(f"Agent资源已释放 for session {session_id}")
        
    except Exception as e:
        logging.error(f"Cancel failed for {session_id}: {str(e)}")
        return {"status": "error", "message": str(e)}

# -------------------- 页面/重置 --------------------
@app.get("/")
async def index(request: Request):
    logging.info("Index page requested")
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/reset")
async def web_reset():
    global_variables.reset()
    return {"status": "reset"}

@app.get("/proxy/")
async def proxy_file(url: str = Query(...)):
    decoded_url = urllib.parse.unquote(url)

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(decoded_url, timeout=30.0)
    except Exception as e:
        return Response(content=f"Proxy error: {str(e)}", status_code=502)

    # 强制识别扩展名
    ext = Path(decoded_url).suffix.lower()
    content_type = mimetypes.types_map.get(ext, "application/octet-stream")

    return StreamingResponse(
        iter([resp.content]),
        media_type=content_type,
        headers={"Access-Control-Allow-Origin": "*"}
    )

def build_enhanced_prompt(base_prompt: str, images: list) -> str:
    """构建包含图片信息的增强prompt"""
    if not images:
        return base_prompt
    
    image_descriptions = []
    for img in images:
        desc = (
            f"\n[图片名字: {img['original_name']} "
            f"(url: {img['file_path']}, "
        )
        image_descriptions.append(desc)
    
    return (
        f"{base_prompt}\n\n"
        f"### 附加图片信息({len(images)}张):\n"
        f"{''.join(image_descriptions)}\n\n"
        "请对上述图片内容进行损伤检测。"
    )

def format_file_size(size: int) -> str:
    """格式化文件大小显示"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"

# -------------------- 启动 --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8009)