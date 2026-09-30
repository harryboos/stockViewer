"""Short source diagnostics safe to return without upstream URLs or credentials."""
from __future__ import annotations


class BoardSourceError(RuntimeError):
    """Only contains locally constructed, credential-free source descriptions."""


def source_failure_reason(error: Exception) -> str:
    detail = str(error).lower()
    if "remotedisconnected" in detail or "remote end closed" in detail or "connection reset" in detail:
        return "远端中断连接"
    if "proxy" in detail:
        return "代理连接失败"
    if "name resolution" in detail or "getaddrinfo" in detail or "nodename nor servname" in detail:
        return "域名解析失败"
    if "timed out" in detail or "timeout" in type(error).__name__.lower() or "超时" in detail:
        return "请求超时"
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return f"行情服务返回 HTTP {status}"
    if "分页" in detail or "不完整行情" in detail:
        return "分页数据不完整"
    if "没有返回行情" in detail or "空数据" in detail:
        return "返回空数据"
    if "无效数据" in detail or "格式" in detail or isinstance(error, (ValueError, TypeError, KeyError)):
        return "返回数据格式异常"
    return "行情连接或读取失败"
