"""Conservative read-only refinement; not an atomic deployment lease or executor."""
import json,os,subprocess,sys,datetime
from pathlib import Path

EXPECTED_USER_UNITS=frozenset('xdn-task-markethub_global_data_update'+suffix+'.service' for suffix in ('','_0030','_0400'))

def _idle(unit):
    return (unit.get('active'),unit.get('sub')) in {('inactive','dead'),('failed','failed')} and unit.get('main_pid')=='0' and unit.get('control_pid')=='0' and unit.get('job','') in ('',None)

def refine_gate(base,system_units,user_units):
    result=dict(base)
    tasks=base.get('latest_owner_task')
    terminal=isinstance(tasks,list) and bool(tasks) and all(isinstance(t,dict) and t.get('status') in ('success','failed') and bool(t.get('finished_at')) for t in tasks)
    shape=(isinstance(system_units,list) and bool(system_units) and isinstance(user_units,list) and bool(user_units))
    if shape:
        shape=all(isinstance(u,dict) and isinstance(u.get('unit'),str) for u in system_units+user_units)
    if shape:
        system_names=[u['unit'] for u in system_units];user_names=[u['unit'] for u in user_units]
        shape=(len(set(system_names))==len(system_names) and len(set(user_names))==len(user_names)
               and 'markethub-api.service' in system_names and EXPECTED_USER_UNITS.issubset(user_names)
               and all(n.startswith('markethub') and n.endswith('.service') for n in system_names)
               and all(n.startswith('xdn-task-markethub') and n.endswith('.service') for n in user_names))
    busy_system=[];busy_user=[];api_matches=False
    if shape:
        for unit in system_units:
            if unit['unit']=='markethub-api.service':
                api=base.get('api',{})
                api_matches=(unit.get('active')=='active' and unit.get('sub')=='running'
                    and isinstance(unit.get('main_pid'),str) and unit['main_pid'].isdigit()
                    and int(unit['main_pid'])>0 and unit['main_pid']==api.get('MainPID')
                    and api.get('ActiveState')=='active' and unit.get('control_pid')=='0'
                    and unit.get('job','') in ('',None))
            elif not _idle(unit):busy_system.append(unit)
        busy_user=[u for u in user_units if not _idle(u)]
    additional=(terminal and shape and api_matches and not busy_system and not busy_user
                and base.get('pending_due_owner_path_exists') is False)
    safe=additional and base.get('safe_to_switch_release') is True and base.get('safe_to_launch_second_writer') is True
    result.update({'gate_version':2,'latest_owner_tasks_terminal':terminal,'unit_listing_complete_for_required_scopes':bool(shape),
        'same_api_identity_rechecked':api_matches,'active_or_unknown_system_writers':busy_system,
        'active_or_unknown_user_writers':busy_user,'all_system_units':system_units,'all_user_units':user_units,
        'safe_to_switch_release':bool(safe),'safe_to_launch_second_writer':bool(safe),
        'warning':'read-only conservative snapshot only; requires fresh real external lease and timer quiescence before any cutover. Failed observation is NOT a terminal owner.'})
    return result

def _run(args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30).stdout

def _manager(user):
    if not user:return ['systemctl']
    if os.geteuid()==1000:return ['systemctl','--user']
    if os.geteuid()==0:return ['sudo','-n','-u','yosef','env','XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus','systemctl','--user']
    raise PermissionError('unrecognized user-manager identity')

def _units(user):
    import re
    manager=_manager(user);pattern='xdn-task-markethub*' if user else 'markethub*'
    rows=json.loads(_run(manager+['list-units','--all','--type=service','--output=json','--no-pager',pattern]))
    if not isinstance(rows,list):raise ValueError('unit inventory must be a list')
    result=[]
    for unit in rows:
        name=unit['unit']
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_@.\-]+\.service',name):raise ValueError('invalid unit identifier')
        props={}
        for line in _run(manager+['show',name,'-p','MainPID','-p','ControlPID','-p','ActiveState','-p','SubState']).splitlines():
            key,value=line.split('=',1)
            if key in props:raise ValueError('duplicate service property')
            props[key]=value
        if set(props)!={'MainPID','ControlPID','ActiveState','SubState'}:raise ValueError('missing service properties')
        result.append({'unit':name,'active':props['ActiveState'],'sub':props['SubState'],
                       'main_pid':props['MainPID'],'control_pid':props['ControlPID'],'job':unit.get('job','')})
    return result

if __name__=='__main__':
    try:
        root=Path(__file__).resolve().parent.parent
        if os.name!='posix' or root!=Path('/data/MarketHub2/staging/spec36-20261029-provider-be51e40e'):
            raise PermissionError('read-only probe is bound to approved Linux staging')
        base=json.loads(_run([sys.executable,str(root/'read_only_safe_switch_gate.py')]))
        result=refine_gate(base,_units(False),_units(True))
        result['refined_at_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        print(json.dumps(result,indent=2,ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'gate_version':2,'safe_to_switch_release':False,'safe_to_launch_second_writer':False,
                          'error_type':type(exc).__name__,'warning':'observation failed; not evidence of terminal process or safe cutover'}))
        raise SystemExit(2)