"""Isolated MySQL 4 fixture; never opens TCP or the original database directory."""

from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import subprocess
import time
import uuid


@contextmanager
def isolated_mysql(root: Path, directory: Path):
    """Start a disposable named-pipe server on copied NPC tables and always stop it on exit."""
    directory = directory.resolve()
    if not directory.is_relative_to((root / 'output').resolve()):
        raise ValueError('Fixture must stay inside project output.')
    data = directory / 'data/swdol'
    data.mkdir(parents=True, exist_ok=False)
    (directory / 'tmp').mkdir()
    for path in (root / 'output/npc-investigation-20261103/npc-source').iterdir():
        if path.suffix.lower() in ('.frm', '.myd', '.myi'):
            shutil.copyfile(path, data / path.name)
    pipe = 'CodexCreature' + uuid.uuid4().hex
    env = os.environ.copy()
    for name in ('MYSQL_PWD', 'MYSQL_HOST', 'MYSQL_TCP_PORT', 'MYSQL_UNIX_PORT', 'MYSQL_HOME'):
        env.pop(name, None)
    args = [str(root / 'XYJ_SEVVER/mysql/bin/mysqld-nt.exe'), '--no-defaults', '--standalone', '--console',
            '--basedir=' + str(root / 'XYJ_SEVVER/mysql'), '--datadir=' + str(directory / 'data'),
            '--skip-grant-tables', '--skip-networking', '--enable-named-pipe', '--socket=' + pipe,
            '--skip-innodb', '--skip-bdb', '--pid-file=' + str(directory / 'reader.pid'),
            '--tmpdir=' + str(directory / 'tmp'), '--log-error=' + str(directory / 'error.log')]
    def query(sql):
        """Execute a fixture-owned statement only through this unique local named pipe."""
        result = subprocess.run([str(root / 'XYJ_SEVVER/mysql/bin/mysql.exe'), '--no-defaults', '--pipe',
                                 '--socket=' + pipe, '--user=root', '--batch', '--raw', '--skip-column-names',
                                 'swdol', '--execute=' + sql], env=env, capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('Isolated SQL failed: ' + result.stderr.decode('gbk', errors='replace'))
        return result.stdout.decode('ascii')
    with (directory / 'console.log').open('wb') as log:
        process = subprocess.Popen(args, env=env, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            for _ in range(30):
                if process.poll() is not None:
                    raise RuntimeError('Isolated MySQL exited; inspect fixture log.')
                try:
                    query('SELECT 1;')
                    break
                except RuntimeError:
                    time.sleep(.2)
            else:
                raise RuntimeError('Isolated MySQL did not become ready.')
            query.pipe_name = pipe
            yield query
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
