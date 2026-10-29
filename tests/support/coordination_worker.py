import sys
from pathlib import Path

from retailq.coordination import Coordinator, RuntimeIdentity


def main():
    root = Path(sys.argv[1])
    identity = RuntimeIdentity('127.0.0.1', 3307, 'retailq_test', 'http://127.0.0.1:19531', root / 'knowledge')
    coordinator = Coordinator(root / 'locks', identity)
    if len(sys.argv) > 2 and sys.argv[2] == 'counter':
        for _ in range(30):
            with coordinator.short_lock('audit'):
                path = root / 'counter.txt'
                value = int(path.read_text()) if path.exists() else 0
                path.write_text(str(value + 1))
        return
    lease = coordinator.try_acquire('maintenance')
    print('acquired' if lease else 'busy', flush=True)
    if lease:
        sys.stdin.readline()
        lease.close()


if __name__ == '__main__':
    main()
