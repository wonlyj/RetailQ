import os
from pathlib import Path

from retailq.errors import ServiceError


class DirectoryOwner:
    """OS lock, released even if its process is terminated. Never delete locks."""
    def __init__(self,root: Path):
        self.root=Path(root).resolve()
        self.file=None

    def __enter__(self):
        if self.file is not None:
            raise ServiceError('knowledge_directory_busy',409,'知识目录正在使用中，请先停止其他知识进程。')
        self.root.mkdir(parents=True,exist_ok=True)
        handle=(self.root/'owner.lock').open('a+b')
        if os.fstat(handle.fileno()).st_size==0:
            handle.write(b'\0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ServiceError('knowledge_directory_busy',409,'知识目录正在使用中，请先停止其他知识进程。') from None
        self.file=handle
        return self

    def __exit__(self,*_):
        if self.file is None:return
        try:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
        finally:
            self.file.close();self.file=None
