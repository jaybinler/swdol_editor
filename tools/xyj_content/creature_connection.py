"""Private pinned MySQL client used by GUI and CLI publication; credentials never leave the process."""

import os
import re
import subprocess

from .binary import ContentError,read_bytes
from .database import MYSQL_CLIENT_SHA256,connection_binding
from .files import sha256
from .settings import normalize_config,input_path
from .secrets_windows import unprotect_password


def database_query(root,document):
    """Create a fixed-workflow client from saved settings and protected credentials, without logging secrets."""
    config=normalize_config(root,document['config']);db=config['database']
    client=input_path(root,db['client'])
    if sha256(read_bytes(client)) != MYSQL_CLIENT_SHA256:
        raise ContentError('MySQL 客户端版本未核验。')
    password=os.environ.get('XYJ_MYSQL_PASSWORD','');filename=document.get('secret_file')
    if not password and filename:
        if not isinstance(filename,str) or not re.fullmatch(r'[0-9a-f]{32}\.bin',filename):
            raise ContentError('密码保护文件索引无效。')
        password=unprotect_password(read_bytes(root/'output/editor/secrets'/filename),connection_binding(db))
    args=[str(client),'--no-defaults','--user='+db['user'],'--batch','--raw','--skip-column-names',
          '--connect-timeout='+str(db['timeout'])]
    args+=['--pipe','--socket='+db['pipe']] if db['transport']=='pipe' else ['--host='+db['host'],'--port='+str(db['port'])]
    args+=[db['name']]
    env=os.environ.copy()
    for key in ('MYSQL_PWD','MYSQL_HOST','MYSQL_TCP_PORT','MYSQL_UNIX_PORT','MYSQL_HOME','XYJ_MYSQL_PASSWORD'):
        env.pop(key,None)
    env['MYSQL_PWD']=password
    def query(sql):
        """Execute publisher-owned SQL only; do not expose raw SQL diagnostics or passwords."""
        try:
            result=subprocess.run(args+['--execute='+sql],env=env,capture_output=True,timeout=db['timeout']+20,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except (OSError,subprocess.TimeoutExpired) as exc:
            raise ContentError('数据库连接失败或超时，恢复日志保留。') from exc
        if result.returncode:
            code=re.search(rb'ERROR\s+(\d+)',result.stderr)
            raise ContentError('数据库拒绝执行，错误码 '+(code.group(1).decode() if code else '未知')+'；请在设置核对连接并保护保存密码。')
        if len(result.stdout)>32*1024*1024:
            raise ContentError('数据库返回超过上限。')
        return result.stdout.decode('ascii')
    return config,query
