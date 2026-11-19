#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本地的文件上传到 HEC1020 板卡的指定目录。

*** 最常改的两个参数在下面的『配 置 区』里（不在别处）： ***
        LOCAL_FILE = "study.py"           # 要传哪个文件
        REMOTE_DIR = "/userapp/userdata"   # 传到板卡的哪个目录
    改完直接运行 python send_to_hec.py 即可；临时也可以命令行覆盖
    （--local / -r）。qq

HEC1020 是 Linux 主机（以太网默认 192.168.1.253，WiFi 默认 192.168.2.253），
出厂用户 root / 密码 hec999，支持 ftp、tftp、scp。

用法示例:
    python send_to_hec.py                       # 默认 auto：依次尝试 sftp -> scp -> cat
    python send_to_hec.py -H 192.168.2.253      # 用 WiFi 时的 IP
    python send_to_hec.py -p hec999 -r /userapp/tmp/
    python send_to_hec.py --local test.py -r /userapp/userdata
    python send_to_hec.py --method scp          # 板卡 sshd 没配 sftp subsystem 时
    python send_to_hec.py --method ftp          # 板卡开了 ftp 服务时

注意: HEC1020(Buildroot) 的 /etc/ssh/sshd_config 默认没有
    Subsystem sftp /usr/libexec/sftp-server
这一行，所以 SFTP 通道会被服务端立即关闭（paramiko 报 "Channel closed"）。
此时用 scp / cat 方式即可（只走 ssh exec 通道，不需要 sftp 子系统）。
"""

import argparse
import hashlib
import os
import posixpath
import sys
import time

# =============================== 配 置 区 ===============================
# 平时只改下面两行就够了，改完直接运行:  python send_to_hec.py
LOCAL_FILE = "study.py"            # 要上传的本地文件：相对本脚本目录，或写绝对路径
REMOTE_DIR = "/userapp/userdata"  # 上传到板卡的哪个目录（不存在会自动创建）

# 板卡连接信息，一般不用动
DEFAULT_HOST = "192.168.1.253"   # 以太网；WiFi 方式登录用 192.168.2.253
DEFAULT_USER = "root"
DEFAULT_PASS = "hec999"          # 出厂密码，建议登录后修改
# ======================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_local(path: str) -> str:
    """把 LOCAL_FILE 解析成绝对路径。
    绝对路径原样返回；相对路径先按『脚本所在目录』找，找不到再按当前工作目录找。
    """
    if os.path.isabs(path):
        return path
    cand = os.path.join(SCRIPT_DIR, path)
    if os.path.isfile(cand):
        return cand
    return os.path.abspath(path)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def local_md5(path: str) -> str:
    """计算本地文件 MD5"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


# --------------------------------------------------------------------------- #
# 方式一：SFTP（基于 paramiko，推荐）
# --------------------------------------------------------------------------- #
def upload_sftp(local, remote_dir, remote_name, args):
    remote_path = posixpath.join(remote_dir, remote_name)
    total = os.path.getsize(local)
    t0 = time.time()

    print(f"[1/4] 连接 {args.user}@{args.host}:{args.port} (SFTP) ...")
    client = _ssh_connect(args)

    try:
        try:
            sftp = client.open_sftp()
        except Exception as e:
            raise RuntimeError(
                f"服务端未开启 sftp 子系统 ({type(e).__name__}: {e})。\n"
                "      可在板卡上执行:\n"
                "        echo 'Subsystem sftp /usr/libexec/sftp-server' >> /etc/ssh/sshd_config\n"
                "        /etc/init.d/S50sshd restart\n"
                "      或直接改用: python send_to_hec.py --method scp"
            ) from e

        # 目标目录不存在则逐级创建
        print(f"[2/4] 检查远程目录 {remote_dir} ...")
        if not _sftp_exists(sftp, remote_dir):
            _sftp_makedirs(sftp, remote_dir)

        # 上传并显示进度
        print(f"[3/4] 上传 {local} -> {remote_path} ({human_size(total)}) ...")
        last = [-1]

        def _progress(sent, size):
            pct = int(sent * 100 / size) if size else 100
            if pct != last[0]:
                last[0] = pct
                bar = "#" * (pct // 4)
                print(f"\r      [{bar:<25}] {pct:3d}%  {human_size(sent)}",
                      end="", flush=True)

        sftp.put(local, remote_path, callback=_progress)
        print()

        # 用 md5sum 校验（板卡没有该命令时退回按大小校验）
        print("[4/4] 校验文件完整性 ...")
        _verify(client, local, remote_path, total)

        # 顺手给个可执行权限，方便 python3 /userapp/tmp/study.py 或直接运行
        try:
            sftp.chmod(remote_path, 0o755)
        except Exception:
            pass

        sftp.close()
        size = _remote_size(client, remote_path)
        if size is not None and size != total:
            raise RuntimeError(f"远程文件大小 {size} != 本地 {total}")

    finally:
        client.close()

    print(f"完成: {remote_path}  用时 {time.time() - t0:.1f}s")
    print(f"提示: 板卡上执行 -> cd {remote_dir} && python3 {remote_name}")
    return remote_path


def _ssh_connect(args):
    """建立 SSH 连接（sftp / scp / cat 三种方式共用）"""
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("未安装 paramiko，请先执行: pip install paramiko")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.host,
        port=args.port,
        username=args.user,
        password=args.password,
        timeout=args.timeout,
        banner_timeout=args.timeout,
        auth_timeout=args.timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def _run_cmd(client, cmd, timeout=15):
    """执行一条远程命令，返回 (返回码, stdout, stderr)"""
    _, out, err = client.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    return rc, out.read().decode(errors="ignore"), err.read().decode(errors="ignore")


def _ensure_remote_dir(client, remote_dir):
    rc, _, err = _run_cmd(client, f"mkdir -p {remote_dir}")
    if rc != 0:
        raise RuntimeError(f"创建远程目录 {remote_dir} 失败: {err.strip()}")


def _verify(client, local, remote_path, total):
    """上传后校验：优先比对 md5，没有 md5sum 就比对文件大小"""
    local_sum = local_md5(local)
    remote_sum = _remote_md5(client, remote_path)
    if remote_sum is None:
        size = _remote_size(client, remote_path)
        if size is None:
            print("      板卡上没有 md5sum/stat，跳过校验")
        elif size == total:
            print(f"      板卡上没有 md5sum，按文件大小校验通过 ({size}B)")
        else:
            raise RuntimeError(f"校验失败: 远程大小 {size} != 本地 {total}")
    elif remote_sum.lower() == local_sum.lower():
        print(f"      校验通过 MD5={local_sum}")
    else:
        raise RuntimeError(f"校验失败: 本地 {local_sum} != 远程 {remote_sum}")


def _push_chunks(chan, local, total):
    """把本地文件逐块写进 ssh channel，并打印进度"""
    sent = 0
    last = -1
    with open(local, "rb") as f:
        while True:
            chunk = f.read(32 * 1024)
            if not chunk:
                break
            chan.sendall(chunk)
            sent += len(chunk)
            pct = int(sent * 100 / total) if total else 100
            if pct != last:
                last = pct
                bar = "#" * (pct // 4)
                print(f"\r      [{bar:<25}] {pct:3d}%  {human_size(sent)}",
                      end="", flush=True)
    print()


def _sftp_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def _sftp_makedirs(sftp, path):
    parts = [p for p in path.split("/") if p]
    cur = "/" if path.startswith("/") else ""
    for p in parts:
        cur = posixpath.join(cur, p) if cur else p
        if not _sftp_exists(sftp, cur):
            sftp.mkdir(cur)


def _remote_md5(client, remote_path):
    for cmd in (f"md5sum {remote_path}", f"md5 {remote_path}"):
        try:
            _, out, err = client.exec_command(cmd, timeout=10)
            text = out.read().decode(errors="ignore").strip()
            if not text and err.read():
                continue
            token = text.split()[0] if text else ""
            if len(token) == 32:
                return token
        except Exception:
            continue
    return None


def _remote_size(client, remote_path):
    try:
        _, out, _ = client.exec_command(f"stat -c %s {remote_path}", timeout=10)
        return int(out.read().decode().strip())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 方式二：SCP（legacy scp 协议，只走 ssh exec 通道，不需要 sftp 子系统）
# --------------------------------------------------------------------------- #
def upload_scp(local, remote_dir, remote_name, args):
    remote_path = posixpath.join(remote_dir, remote_name)
    total = os.path.getsize(local)
    t0 = time.time()

    print(f"[1/4] 连接 {args.user}@{args.host}:{args.port} (SCP) ...")
    client = _ssh_connect(args)
    try:
        print(f"[2/4] 检查远程目录 {remote_dir} ...")
        _ensure_remote_dir(client, remote_dir)

        print(f"[3/4] 上传 {local} -> {remote_path} ({human_size(total)}) ...")
        chan = client.get_transport().open_session()
        chan.settimeout(max(args.timeout, 30))
        chan.exec_command(f"scp -t {remote_dir}")
        resp = chan.recv(1)
        if resp != b"\x00":
            raise RuntimeError(f"scp -t 启动失败: {_scp_error(chan, resp)}")

        chan.sendall(f"C0644 {total} {remote_name}\n".encode())
        resp = chan.recv(1)
        if resp != b"\x00":
            raise RuntimeError(f"scp 拒绝文件名 {remote_name}: {_scp_error(chan, resp)}")

        _push_chunks(chan, local, total)
        chan.sendall(b"\x00")
        resp = chan.recv(1)
        if resp != b"\x00":
            raise RuntimeError(f"scp 写入失败: {_scp_error(chan, resp)}")
        chan.sendall(b"\x01")          # 结束本次传输
        chan.close()

        print("[4/4] 校验文件完整性 ...")
        _verify(client, local, remote_path, total)
        _run_cmd(client, f"chmod 755 {remote_path}")
    finally:
        client.close()

    print(f"完成: {remote_path}  用时 {time.time() - t0:.1f}s")
    print(f"提示: 板卡上执行 -> cd {remote_dir} && python3 {remote_name}")
    return remote_path


def _scp_error(chan, first):
    """读一下 scp 返回的非 0 字节后面跟着的提示文字"""
    try:
        text = first + chan.recv(1024)
    except Exception:
        text = first
    return text.decode(errors="ignore").strip()


# --------------------------------------------------------------------------- #
# 方式三：exec + `cat > file`，最原始但最通用的推流方式
# --------------------------------------------------------------------------- #
def upload_cat(local, remote_dir, remote_name, args):
    remote_path = posixpath.join(remote_dir, remote_name)
    total = os.path.getsize(local)
    t0 = time.time()

    print(f"[1/4] 连接 {args.user}@{args.host}:{args.port} (cat) ...")
    client = _ssh_connect(args)
    try:
        print(f"[2/4] 检查远程目录 {remote_dir} ...")
        _ensure_remote_dir(client, remote_dir)

        print(f"[3/4] 上传 {local} -> {remote_path} ({human_size(total)}) ...")
        chan = client.get_transport().open_session()
        chan.settimeout(max(args.timeout, 30))
        chan.exec_command(f"cat > {remote_path}")
        _push_chunks(chan, local, total)
        chan.shutdown_write()
        rc = chan.recv_exit_status()
        chan.close()
        if rc != 0:
            raise RuntimeError(f"板卡上 cat > {remote_path} 返回码 {rc}")

        print("[4/4] 校验文件完整性 ...")
        _verify(client, local, remote_path, total)
        _run_cmd(client, f"chmod 755 {remote_path}")
    finally:
        client.close()

    print(f"完成: {remote_path}  用时 {time.time() - t0:.1f}s")
    print(f"提示: 板卡上执行 -> cd {remote_dir} && python3 {remote_name}")
    return remote_path

# --------------------------------------------------------------------------- #
# 方式四：FTP（板卡开了 ftpd 时使用，ftplib 是标准库）
# --------------------------------------------------------------------------- #
def upload_ftp(local, remote_dir, remote_name, args):
    from ftplib import FTP, error_perm

    remote_path = posixpath.join(remote_dir, remote_name)
    total = os.path.getsize(local)
    t0 = time.time()

    print(f"[1/3] 连接 ftp://{args.user}@{args.host}:{args.port} ...")
    ftp = FTP()
    ftp.connect(args.host, args.port, timeout=args.timeout)
    ftp.login(args.user, args.password)

    try:
        print(f"[2/3] 检查远程目录 {remote_dir} ...")
        ftp.cwd("/")
        for part in [p for p in remote_dir.split("/") if p]:
            try:
                ftp.cwd(part)
            except error_perm:
                ftp.mkd(part)
                ftp.cwd(part)

        print(f"[3/3] 上传 {local} -> {remote_path} ({human_size(total)}) ...")
        with open(local, "rb") as f:
            ftp.storbinary(f"STOR {remote_name}", f)
    finally:
        ftp.quit()

    print(f"完成: {remote_path}  用时 {time.time() - t0:.1f}s")
    return remote_path


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description=f"把本地文件上传到 HEC1020（当前配置: "
                    f"{LOCAL_FILE} -> {REMOTE_DIR}）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
默认值来自脚本顶部的『配置区』: LOCAL_FILE={LOCAL_FILE}  REMOTE_DIR={REMOTE_DIR}

示例:
  python send_to_hec.py
  python send_to_hec.py -H 192.168.2.253 -u root -p hec999
  python send_to_hec.py --local test.py --remote-dir /userapp/userdata
  python send_to_hec.py --method ftp
""",
    )
    parser.add_argument("-H", "--host", default=DEFAULT_HOST,
                        help=f"HEC1020 的 IP（默认 {DEFAULT_HOST}，WiFi 为 192.168.2.253）")
    parser.add_argument("-u", "--user", default=DEFAULT_USER, help="登录用户名")
    parser.add_argument("-p", "--password", default=DEFAULT_PASS, help="登录密码")
    parser.add_argument("--local", default=LOCAL_FILE,
                        help=f"本地文件路径（默认取配置区 LOCAL_FILE={LOCAL_FILE}）")
    parser.add_argument("-r", "--remote-dir", default=REMOTE_DIR,
                        help=f"远程目录（默认取配置区 REMOTE_DIR={REMOTE_DIR}）")
    parser.add_argument("--remote-name", default=None, help="远程文件名（默认与本地同名）")
    parser.add_argument("--method", choices=("auto", "sftp", "scp", "cat", "ftp"),
                        default="auto",
                        help="传输方式：auto(默认,依次尝试 sftp->scp->cat) / "
                             "sftp / scp / cat (都走端口22) / ftp(端口21)")
    parser.add_argument("--port", type=int, default=None, help="自定义端口")
    parser.add_argument("-t", "--timeout", type=float, default=15.0, help="超时秒数")
    args = parser.parse_args()

    if args.port is None:
        args.port = 21 if args.method == "ftp" else 22
    args.local = resolve_local(args.local)
    if args.remote_name is None:
        args.remote_name = os.path.basename(args.local)

    if not os.path.isfile(args.local):
        print(f"错误: 本地文件不存在: {args.local}", file=sys.stderr)
        print("      请修改脚本顶部『配置区』的 LOCAL_FILE，或用 --local 临时指定",
              file=sys.stderr)
        return 1

    uploaders = {"sftp": upload_sftp, "scp": upload_scp, "cat": upload_cat}

    try:
        if args.method == "ftp":
            upload_ftp(args.local, args.remote_dir, args.remote_name, args)
        elif args.method == "auto":
            errors = []
            for name in ("sftp", "scp", "cat"):
                try:
                    uploaders[name](args.local, args.remote_dir, args.remote_name, args)
                    break
                except Exception as e:
                    errors.append(f"{name}: {type(e).__name__}: {e}")
                    print(f"[auto] {name} 方式不可用（{type(e).__name__}: {e}），换下一种 ...",
                          flush=True)
                    print(flush=True)
            else:
                raise RuntimeError("三种 SSH 方式都失败了:\n  " + "\n  ".join(errors))
        else:
            uploaders[args.method](args.local, args.remote_dir, args.remote_name, args)
    except Exception as e:
        print(f"\n失败: {type(e).__name__}: {e}", file=sys.stderr)
        if args.method != "ftp":
            print("提示: 若板卡未开 SSH，可改用 --method ftp", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
