"""用匿名标准输入管道向购买进程传入临时密码。"""
import os
import subprocess

from core.errors import StopOrder


def launch_worker(command, cwd, env, output, payment_secret):
    command = list(command)
    if payment_secret:
        command.append("--payment-secret-stdin")
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if payment_secret else subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if payment_secret:
        try:
            process.stdin.write(payment_secret.pipe_payload())
            process.stdin.close()
        except Exception:
            process.terminate()
            process.wait(timeout=5)
            raise StopOrder("购买进程未接收本次支付密码，请重新启动任务") from None
    return process
