"""校验审核快照中的 B 站投稿参数。"""
from .publishing import WorkflowError


def upload_options(options: dict) -> list[str]:
    tid = options.get("bilibili_tid")
    copyright = options.get("bilibili_copyright")
    if type(tid) is not int or not 1 <= tid <= 99999:
        raise WorkflowError("请选择 B 站投稿分区，或填写有效的分区 ID。", 422)
    if type(copyright) is not int or copyright not in {1, 2}:
        raise WorkflowError("请选择 B 站版权类型：原创或转载。", 422)
    args = ["--tid", str(tid), "--copyright", str(copyright)]
    if copyright == 2:
        source = options.get("bilibili_source")
        if not isinstance(source, str) or not source.strip() or len(source) > 2000:
            raise WorkflowError("转载作品必须填写来源（最多 2000 字符）。", 422)
        args.extend(["--source", source.strip()])
    return args
