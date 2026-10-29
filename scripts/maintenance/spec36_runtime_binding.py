"""SPEC36 Linux timer / private receipt IO. No CLI or construction side effects."""
import json
import os
import re
import subprocess

class BindingError(RuntimeError):pass

def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise BindingError('duplicate input key')
        result[key]=value
    return result

class LinuxTimerIO:
    PROPERTIES=('ActiveState','SubState','RequiredBy','BoundBy','ConsistsOf','PropagatesStopTo','Job')
    def __init__(self,*,run=None,getuid=None):
        self.run=subprocess.run if run is None else run
        self.getuid=os.geteuid if getuid is None else getuid
        if not callable(self.run) or not callable(self.getuid):raise ValueError('explicit callable IO seams required')
        self.seen=frozenset()
    def _manager(self,scope):
        uid=self.getuid()
        if type(uid) is not int or uid not in (0,1000) or scope not in ('system','user'):
            raise BindingError('explicit system or yosef manager required')
        if scope=='system':return ('/usr/bin/systemctl',)
        if uid==1000:return ('/usr/bin/systemctl','--user')
        return ('/usr/bin/sudo','-n','-u','yosef','/usr/bin/env','XDG_RUNTIME_DIR=/run/user/1000',
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus','/usr/bin/systemctl','--user')
    def _run(self,scope,args):
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C','LANG':'C','SYSTEMD_PAGER':'',
             'SYSTEMD_COLORS':'0','SYSTEMD_PAGERSECURE':'1','XDG_RUNTIME_DIR':'/run/user/1000',
             'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus'}
        try:
            result=self.run(self._manager(scope)+tuple(args),shell=False,check=True,timeout=30,
              stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,close_fds=True)
            if type(result.returncode) is not int or result.returncode!=0 or type(result.stdout) is not bytes or len(result.stdout)>262144:
                raise BindingError('invalid timer command result')
            return result.stdout
        except Exception:raise BindingError('timer manager IO failed') from None
    @staticmethod
    def _name(scope,name):
        prefix='markethub' if scope=='system' else 'xdn-task-markethub'
        return scope in ('system','user') and isinstance(name,str) and name.startswith(prefix) and re.fullmatch(r'[A-Za-z0-9_@.\-]+\.timer',name) is not None
    def _show(self,scope,name):
        props={}
        args=['show',name]
        for key in self.PROPERTIES:args.extend(('-p',key))
        for line in self._run(scope,args).decode('ascii').splitlines():
            key,value=line.split('=',1)
            if key in props:raise BindingError('duplicate timer property')
            props[key]=value
        if set(props)!=set(self.PROPERTIES):raise BindingError('incomplete timer properties')
        if not all(re.fullmatch(r'[a-z][a-z-]{0,63}',props[k]) for k in ('ActiveState','SubState')):
            raise BindingError('invalid timer state')
        return props
    @staticmethod
    def _dependents(props):
        return sorted(set(name for key in ('RequiredBy','BoundBy','ConsistsOf','PropagatesStopTo') for name in props[key].split()))
    def inventory(self):
        self.seen=frozenset()  # failed/partial refresh cannot retain old mutation authority
        try:
            result=[];seen=set()
            for scope,pattern in (('system','markethub*.timer'),('user','xdn-task-markethub*.timer')):
                rows=json.loads(self._run(scope,('list-units','--all','--type=timer','--output=json','--no-pager',pattern)),object_pairs_hook=_object)
                if not isinstance(rows,list) or not 0<len(rows)<=64:raise BindingError('invalid timer inventory')
                for row in rows:
                    name=row['unit'];key=(scope,name)
                    if not self._name(scope,name) or key in seen:raise BindingError('invalid timer unit identity')
                    props=self._show(scope,name)
                    result.append({'scope':scope,'unit':name,'active':props['ActiveState'],'sub':props['SubState'],
                      'job':'' if props['Job'] in ('','0 /') else 'pending','stop_dependents':self._dependents(props)})
                    seen.add(key)
            self.seen=frozenset(seen)
            return result
        except Exception:raise BindingError('timer inventory IO failed') from None
    def command(self,scope,verb,name):
        try:
            uid=self.getuid()
            if type(uid) is not int or uid!=0 or verb not in ('start','stop') or not self._name(scope,name) or (scope,name) not in self.seen:
                raise BindingError('root and seen timer-only command required')
            props=self._show(scope,name)
            if props['Job'] not in ('','0 /') or self._dependents(props):raise BindingError('timer mutation boundary is unsafe')
            allowed=(('active','waiting'),('active','elapsed')) if verb=='stop' else (('inactive','dead'),)
            if (props['ActiveState'],props['SubState']) not in allowed:raise BindingError('timer mutation state changed')
            self._run(scope,(verb,name));return True
        except Exception:raise BindingError('timer mutation IO failed') from None