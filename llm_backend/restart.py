import os
import sys
import time
import signal
import subprocess
import threading
from datetime import datetime
import shutil

LOG_FILE = "fastapi_restart.log"

def setup_logging():
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()

def log_message(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] {message}\n"
    with open(LOG_FILE, 'a') as f:
        f.write(log_entry)
    print(log_entry.strip())

def stream_logger(stream, prefix):
    for line in iter(stream.readline, b''):
        decoded = line.decode(errors="replace").rstrip()
        log_message(f"{prefix}: {decoded}")
    stream.close()

def clear_folder_files(folder_path):
    if not os.path.exists(folder_path):
        log_message(f"清理文件夹失败：路径不存在 {folder_path}")
        return
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
                log_message(f"已删除文件: {file_path}")
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
                log_message(f"已删除子文件夹及其内容: {file_path}")
        except Exception as e:
            log_message(f"删除文件/文件夹失败: {file_path}, 错误: {e}")

def force_restart_fastapi(app_path, interval_hours=24, port=8009, cleanup_dirs=None):
    setup_logging()
    log_message(f"启动FastAPI重启守护进程，应用路径: {app_path}, 端口: {port}, 重启间隔: {interval_hours}小时")

    if cleanup_dirs is None:
        cleanup_dirs = []

    while True:
        # 清理所有指定文件夹
        for folder in cleanup_dirs:
            log_message(f"清理目录: {folder}")
            clear_folder_files(folder)

        log_message(f"启动FastAPI应用: {app_path}")
        process = subprocess.Popen(
            [sys.executable, app_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        log_message(f"FastAPI应用启动成功，PID: {process.pid}")

        threading.Thread(target=stream_logger, args=(process.stdout, "STDOUT"), daemon=True).start()
        threading.Thread(target=stream_logger, args=(process.stderr, "STDERR"), daemon=True).start()

        log_message(f"等待 {interval_hours} 小时后重启...")
        time.sleep(interval_hours * 3600)

        log_message("开始强制终止FastAPI应用")
        process.terminate()
        time.sleep(2)
        if process.poll() is None:
            log_message(f"进程未正常终止，强制杀死(PID: {process.pid})")
            process.kill()
            time.sleep(1)

        try:
            log_message(f"检查端口 {port} 上的残留进程")
            result = subprocess.run(
                f"lsof -i :{port} | grep LISTEN | awk '{{print $2}}'",
                shell=True, check=True, capture_output=True, text=True
            )
            pids = result.stdout.split()
            for pid in pids:
                try:
                    log_message(f"发现残留进程(PID: {pid})，尝试终止")
                    os.kill(int(pid), signal.SIGKILL)
                    log_message(f"已强制终止PID {pid}")
                except ProcessLookupError:
                    log_message(f"进程PID {pid} 已不存在")
        except subprocess.CalledProcessError as e:
            log_message(f"检查端口时出错: {str(e)}")

        log_message("FastAPI应用已强制终止")

if __name__ == "__main__":
    force_restart_fastapi(
        app_path="fast_app.py",
        interval_hours=1,
        port=8009,
        cleanup_dirs=["servers/openfast-server/simulation_runs", "servers/openfast-server/plots", "servers/opensees-server/plots"]  # 这里填入多个你想清理的目录
    )
