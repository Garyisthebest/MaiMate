#!/usr/bin/env python3
"""Install the official Cloudflare binary in this project, checking its release digest."""
from __future__ import annotations
import hashlib
import io
import json
import os
import platform
import tarfile
from pathlib import Path

import httpx
from mcd_adapter import ROOT


def main():
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if platform.system() != 'Darwin' or not arch:
        raise SystemExit('当前安装脚本适用于 macOS；请从 Cloudflare 官方发布页获取匹配版本，放入项目 runtime/bin/cloudflared')
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get('https://api.github.com/repos/cloudflare/cloudflared/releases/latest', headers={'Accept': 'application/vnd.github+json'})
        response.raise_for_status()
        release = response.json()
        name = f'cloudflared-darwin-{arch}.tgz'
        asset = next((a for a in release['assets'] if a['name'] == name), None)
        if not asset or not str(asset.get('digest', '')).startswith('sha256:'):
            raise SystemExit('官方发布没有提供可验证的 SHA256，安装已停止')
        download = client.get(asset['browser_download_url'])
        download.raise_for_status()
        if len(download.content) > 100_000_000 or hashlib.sha256(download.content).hexdigest() != asset['digest'].split(':', 1)[1]:
            raise SystemExit('下载内容校验失败，安装已停止')
    directory = ROOT / 'runtime/bin'
    directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(download.content), mode='r:gz') as archive:
        member = next((m for m in archive.getmembers() if m.isfile() and Path(m.name).name == 'cloudflared'), None)
        if member is None:
            raise SystemExit('官方压缩包没有找到可执行文件')
        with archive.extractfile(member) as source:
            data = source.read(120_000_000)
    path = directory / 'cloudflared'
    path.write_bytes(data)
    os.chmod(path, 0o700)
    (directory / 'cloudflared-release.json').write_text(json.dumps({'version': release['tag_name'], 'asset': name, 'digest': asset['digest']}, indent=2))
    print('已安装 Cloudflare 官方定位通道组件：' + release['tag_name'] + '，SHA256 已核对；文件仅在项目 runtime/bin')


if __name__ == '__main__':
    main()
