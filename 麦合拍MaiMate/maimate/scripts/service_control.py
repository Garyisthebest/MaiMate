#!/usr/bin/env python3
"""Project-local background process control, with no system service installation."""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'runtime'
PID_FILE = RUNTIME / 'service.pid'
URL = 'http://127.0.0.1:8765'


def running_pid():
    try:
        result = subprocess.run(['lsof', '-t', '-iTCP:8765', '-sTCP:LISTEN'], capture_output=True, text=True)
        ids = list(set(result.stdout.split()))
        if len(ids) != 1:
            return None
        pid = int(ids[0])
        command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
        if str(ROOT / 'maimate/scripts/preview_server.py') in command:
            return pid
    except (OSError, ValueError):
        pass
    return None


def online():
    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            d = client.get(URL + '/api/status').json()
            return d.get('connected') is True and d.get('mode') == 'ordering_agent'
    except (httpx.HTTPError, ValueError):
        return False


def start():
    pid = running_pid()
    if pid and online():
        print('麦合拍已在后台运行：' + URL)
        return
    env = {**os.environ, 'TMPDIR': str(RUNTIME / 'tmp'), 'XDG_CACHE_HOME': str(RUNTIME / 'cache'), 'PYTHONDONTWRITEBYTECODE': '1'}
    with (RUNTIME / 'preview.log').open('ab') as log:
        os.chmod(RUNTIME / 'preview.log', 0o600)
        process = subprocess.Popen([str(ROOT / '.conda/bin/python'), str(ROOT / 'maimate/scripts/preview_server.py'), '--resume-weixin'],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    PID_FILE.write_text(str(process.pid))
    for _ in range(120):
        if process.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            raise RuntimeError('启动失败。请查看项目 runtime/preview.log；不要公开日志。')
        if running_pid() == process.pid and online():
            print('麦合拍已在后台启动；已有微信绑定会恢复接收。管理台：' + URL)
            return
        time.sleep(.25)
    raise RuntimeError('进程仍在初始化，请稍后执行 ./maimate.sh status 检查。')


def stop():
    pid = running_pid()
    if not pid:
        print('没有本项目在8765端口运行的进程。')
        return
    os.kill(pid, signal.SIGINT)
    for _ in range(80):
        if running_pid() != pid:
            PID_FILE.unlink(missing_ok=True)
            print('麦合拍已停止。')
            return
        time.sleep(.1)
    raise RuntimeError('进程尚未退出；没有强制终止，请稍后再检查。')


def main():
    parser = argparse.ArgumentParser(description='麦合拍本机后台管理')
    parser.add_argument('command', choices=['start', 'stop', 'restart', 'status', 'open'], nargs='?', default='start')
    args = parser.parse_args()
    for name in ('tmp', 'cache'):
        (RUNTIME / name).mkdir(parents=True, exist_ok=True)
    # Serialize concurrent starts without writing launchd/Conda/global settings.
    with (RUNTIME / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if args.command == 'status':
                print('麦合拍运行中：' + URL if running_pid() and online() else '麦合拍未就绪。')
            elif args.command == 'stop':
                stop()
            else:
                if args.command == 'restart':
                    stop()
                start()
                if args.command == 'open':
                    subprocess.run(['open', URL], check=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
