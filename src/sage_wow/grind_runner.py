"""Standalone frozen grinding session; deliberately bypasses the full harness."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time

from sage_wow.agent.grind_only import GrindController, settings
from sage_wow.agent.cycle import CycleResult
from sage_wow.agent.grind_search import catalog_path
from sage_wow.agent.evidence import EvidenceArchive
from sage_wow.config import load_profile
from sage_wow.control.executor import SafeExecutor, ExecutionRejected
from sage_wow.models import Event
from sage_wow.platform.macos.capture import capture_window
from sage_wow.platform.macos.input import QuartzInput
from sage_wow.platform.macos.system import host_checks
from sage_wow.platform.macos.window_gate import current_gate, gate_recovery_kind, identity_window_key
from sage_wow.sage.client import SageClient
from sage_wow.storage import EventStore


PAUSED_STATES={'PAUSED_FOCUS','PAUSED_OPERATOR','PAUSED_HEARTBEAT','PAUSED_IDENTITY','PAUSED_GATE'}


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(root):
    paths=sorted((root/'src'/'sage_wow').rglob('*.py'))
    paths += [root/name for name in ('pyproject.toml','uv.lock') if (root/name).is_file()]
    catalog=root/'profiles'/'hunting-areas.yaml'
    if catalog.is_file():paths.append(catalog)
    return {str(path.relative_to(root)):digest(path) for path in paths}


class GrindFreeze:
    def __init__(self,profile,directory):
        self.profile=profile;self.root=Path(__file__).resolve().parents[2]
        self.catalog=catalog_path(profile).resolve()
        revision=subprocess.run(['git','rev-parse','HEAD'],cwd=self.root,check=True,capture_output=True,text=True).stdout.strip()
        tracked=['src','pyproject.toml','uv.lock','profiles/hunting-areas.yaml']
        if self.catalog.is_relative_to(self.root):tracked.append(str(self.catalog.relative_to(self.root)))
        dirty=subprocess.run(['git','status','--porcelain','--',*tracked],
            cwd=self.root,check=True,capture_output=True,text=True).stdout.strip()
        if dirty:raise ValueError('Commit your changes first: a run records the exact source it ran, so src/, pyproject.toml, uv.lock and the area catalog must be clean in git')
        self.manifest={'mode':'grind_only','session_id':settings(profile)['session_id'],'commit':revision,
            'sources':source_hashes(self.root),'profile_path':str(profile.path.resolve()),'profile_sha256':digest(profile.path),
            'runtime_profile_sha256':hashlib.sha256(json.dumps(profile.values,sort_keys=True).encode()).hexdigest(),
            'area_catalog_path':str(self.catalog),'area_catalog_sha256':digest(self.catalog),
            'claim':'Frozen configuration/source; no gameplay outcome asserted'}
        with (directory/'launch-manifest.json').open('x') as stream:json.dump(self.manifest,stream,indent=2)

    def changed(self):
        if source_hashes(self.root)!=self.manifest['sources']:return 'grind_source_changed'
        if digest(self.catalog)!=self.manifest['area_catalog_sha256']:return 'grind_area_catalog_changed'
        if digest(self.profile.path)!=self.manifest['profile_sha256']:return 'grind_profile_changed'
        if hashlib.sha256(json.dumps(self.profile.values,sort_keys=True).encode()).hexdigest()!=self.manifest['runtime_profile_sha256']:
            return 'grind_runtime_profile_changed'
        return None


class GrindRunner:
    IDLE_GATE_RECOVERIES=2
    IDLE_GATE_RECOVERY_SECONDS=5.0

    def __init__(self,profile,directory,*,sage=None,executor=None,capture=None,ocr=None,freeze_factory=GrindFreeze):
        self.profile=profile;self.config=settings(profile);self.directory=Path(directory)
        self.sage,self.executor,self.capture,self.ocr=sage,executor,capture,ocr
        self.ocr_worker=None
        self.freeze_factory=freeze_factory;self.controller=None;self.store=None;self.reason=None
        self.stopping=False;self.state='STARTING';self.task=None;self.pulse=None
        self.operator_paused=False
        self.invalid_gate=None;self.original_gate_inspector=None
        self.invalid_gate_observation=None;self.gate_failure=None
        self.gate_observation_lock=threading.Lock()
        self.heartbeat_recovery=None;self.recovery_probe_at=None
        self.identity_recovery=None
        self.gate_observation_sequence=0
        self.reconciled_movements={}
        self.periodic_gate_recovery=None
        self.periodic_gate_recoveries=0
        self.gate_retirement_task=None

    def request_stop(self,reason='operator_stop',*,source=None):
        gate_failure=None
        if reason=='focus_or_calibration_invalid' and self.gate_failure is None:
            # Gate inspection may run on an input worker. Persist only here on
            # the runner's event-loop thread, retaining that exact observation.
            with self.gate_observation_lock:
                snapshot=asdict(self.invalid_gate) if self.invalid_gate is not None else None
                observation=self.invalid_gate_observation
                if observation and observation['snapshot']!=snapshot:observation=None
                self.gate_failure={'source':source or sys._getframe(1).f_code.co_name,
                    'stopped_at':time.time(),'runner_state':self.state,
                    'executor_disarm_reason':getattr(self.executor,'disarm_reason',None),
                    'invalid_gate':snapshot,'observation':observation}
                gate_failure=self.gate_failure
        self.stopping=True;self.reason=reason
        if self.controller:self.controller.stop(reason)
        if gate_failure is not None and self.store:self.event('grind_gate_failure',gate_failure)

    async def status(self):
        blocked=self.controller.hunt.blocked if self.controller else None
        reported_state='BLOCKED' if blocked and self.state not in PAUSED_STATES|{'STOPPED'} else self.state
        payload={'state':reported_state,'mode':'grind_only','updated_at':time.time(),'pid':os.getpid(),
            'session_id':self.config['session_id'],'reason':self.reason or (blocked['reason'] if blocked else None),'blocked':blocked,
            'gate_failure':self.gate_failure,
            'player_level':self.controller.level.last_confirmed_level if self.controller else None,
            'goal_level':self.config['goal_level'],
            'deadline':self.controller.deadline if self.controller else None,
            'phase':self.controller.hunt.phase if self.controller else None,
            'recovery':self.controller.hunt.recovery_requested if self.controller else None,
            'navigation_recovery':getattr(self.controller,'navigation_wait',None) if self.controller else None,
            'verified_kills':len(self.controller.hunt.credited_kills) if self.controller else 0}
        # Snapshot controller state on its owning event loop; only immutable
        # status text crosses to the I/O worker. A slow filesystem must not
        # starve the input heartbeat while another task is holding a key.
        text=json.dumps(payload)
        def publish():
            temporary=self.directory/'status.tmp'
            temporary.write_text(text)
            temporary.replace(self.directory/'status.json')
        pending=asyncio.create_task(asyncio.to_thread(publish))
        try:await asyncio.shield(pending)
        except asyncio.CancelledError:
            # to_thread cancellation cannot stop a write already in progress.
            # Drain it before finalization publishes STOPPED, so a late PLAYING
            # snapshot cannot overwrite the final state.
            await pending
            raise

    def event(self,name,payload):self.store.append(Event.create(name,payload))

    def live_dependencies(self):
        if self.sage is not None and self.executor is not None:return
        if self.sage is not None or self.executor is not None:raise ValueError('Supply both fake Sage and executor, or neither')
        required={'operating_system','screen_recording','accessibility'}
        checks={check.name:check.status for check in host_checks()}
        if any(checks.get(name)!='ok' for name in required):raise ValueError('Host permissions required for grinding')
        if not current_gate(self.profile).valid:raise ValueError('Exact foreground window/app/calibration gate must be valid')
        cfg=self.profile.values.get('sage') or {}
        self.sage=SageClient(os.getenv('SAGE_API_KEY',''),endpoint=cfg.get('endpoint','https://sage.levanto.ai'),
            timeout_seconds=cfg.get('request_timeout_seconds',12),max_image_bytes=cfg.get('max_image_bytes',4*1024*1024))
        self.executor=SafeExecutor(QuartzInput(),lambda:current_gate(self.profile),heartbeat_timeout=.75,watchdog_interval=.1)

    async def heartbeat(self):
        expected=time.monotonic();reported=0.
        while not self.stopping:
            now=time.monotonic()
            self.executor.heartbeat(loop_lag_seconds=max(0.,now-expected))
            self.drain_executor_timing()
            if now-reported>=2:
                self.event('grind_runtime_timing',self.executor.timing_snapshot())
                reported=now
            expected=time.monotonic()+.15
            await asyncio.sleep(.15)

    def drain_executor_timing(self):
        # Only this event-loop thread touches the SQLite store. The independent
        # input watcher retains a bounded queue even while this loop is stuck.
        for timing in self.executor.drain_watchdog_events():
            self.event('input_release_timing',timing)
        for timing in self.executor.drain_gate_events():
            self.event('grind_gate_timing',timing)

    @contextmanager
    def phase(self,name):
        started=time.monotonic();self.executor.set_phase(name)
        try:yield
        finally:
            timing=self.executor.timing_snapshot()
            self.event('grind_phase_timing',{**timing,'phase':name,'last_controller_phase':timing['phase'],
                'started_at_monotonic':started,'elapsed_seconds':time.monotonic()-started})
            self.executor.set_phase('idle')

    def control(self):
        path=self.directory/'control.json'
        if not path.exists():return None
        try:
            data=json.loads(path.read_text());command=data.get('command') or data.get('action')
            path.unlink();return command
        except (OSError,ValueError,AttributeError):return None

    async def step(self):
        revision=self.controller.revision
        with self.phase('capture'):
            frame=await asyncio.to_thread(self.capture)
        # Cancelling to_thread cannot stop its worker. A late capture may never
        # become decision evidence after physical authority has been retired.
        if revision!=self.controller.revision or not self.controller.current():
            return CycleResult('invalidated',detail='Capture belongs to retired grind authority')
        with self.phase('frame_archive'):
            frame=replace(frame,image_path=str(self.controller.archive.frame(frame,self.controller.cycle.session_epoch,
                self.controller.cycle.input_generation)))
            self.event('frame_captured',{'frame_id':frame.frame_id,'path':frame.image_path,'width':frame.width,'height':frame.height})
        with self.phase('controller_process'):
            return await self.controller.process(frame)

    def gate_state(self):
        backend=getattr(self.executor,'backend',None)
        owner_check=getattr(backend,'_ensure_owner',None)
        if callable(owner_check):owner_check()
        gate=self.executor.inspect_gate()
        if self.invalid_gate is not None and gate_recovery_kind(self.invalid_gate)=='invalid':return 'invalid'
        return gate_recovery_kind(gate)

    async def poll_gate_state(self):
        backend=getattr(self.executor,'backend',None)
        owner_check=getattr(backend,'_ensure_owner',None)
        if callable(owner_check):owner_check()
        gate=await self.executor.inspect_gate_periodic('runner_gate_state')
        if gate is None:return 'retired'
        if self.invalid_gate is not None and gate_recovery_kind(self.invalid_gate)=='invalid':return 'invalid'
        return gate_recovery_kind(gate)

    def periodic_gate_context(self):
        c=self.controller
        return {'window':self.profile.window_id,'calibration':dict(self.profile.calibration),
            'client':dict(self.profile.values.get('client') or {}),
            'revision':c.revision if c else None,'epoch':c.cycle.session_epoch if c else None,
            'generation':c.cycle.input_generation if c else None}

    def observe_executor_gate(self):
        self.original_gate_inspector=self.executor.inspect_gate
        def inspect():
            caller=sys._getframe(1).f_code.co_name
            gate=self.executor.inspect_gate_measured(self.original_gate_inspector,caller)
            return self.apply_executor_gate(gate,caller)
        self.executor.inspect_gate=inspect
        self.executor.configure_periodic_gate(
            reader=lambda:self.executor.inspect_gate_measured(self.original_gate_inspector,'periodic_native'),
            observer=self.apply_executor_gate,context=self.periodic_gate_context)

    def apply_executor_gate(self,gate,caller):
        with self.gate_observation_lock:
            recovery=self.identity_recovery
        if (recovery is not None and gate_recovery_kind(gate)!='invalid'
                and identity_window_key(gate)!=recovery['window_key']):
            gate=replace(gate,client_identity_verified=False,identity_evidence={
                **(gate.identity_evidence or {}),'reason':'identity_recovery_window_changed',
                'expected_window_key':list(recovery['window_key'])})
        if not gate.valid:
            focus_only=replace(gate,foreground=True).valid
            kind=gate_recovery_kind(gate)
            predicates={'window_present':gate.window_id is not None,
                'window_id_matches':gate.window_id==gate.calibrated_window_id,
                'foreground':gate.foreground,'calibrated':gate.calibrated,
                'client_identity_verified':gate.client_identity_verified,
                'bounds_present':gate.bounds is not None,
                'bounds_match':gate.calibrated_bounds==gate.bounds,
                'positive_dimensions':bool(gate.bounds and gate.bounds.width>0 and gate.bounds.height>0)}
            observation={'snapshot':asdict(gate),'caller':caller,
                'observed_at':time.time(),'monotonic_at':time.monotonic(),
                'thread_name':threading.current_thread().name,'valid':gate.valid,
                'valid_with_foreground':focus_only,
                'failed_predicates':[name for name,value in predicates.items() if not value]}
            with self.gate_observation_lock:
                self.gate_observation_sequence+=1
                if kind=='identity' and self.identity_recovery is None:
                    self.identity_recovery={'window_key':identity_window_key(gate),
                        'observation':observation,'reported':False}
                severity={'valid':0,'focus':1,'identity':2,'invalid':3}
                if self.invalid_gate is None or severity[kind]>severity[gate_recovery_kind(self.invalid_gate)]:
                    self.invalid_gate=gate;self.invalid_gate_observation=observation
            # An observation can originate in a preparation/guard worker,
            # outside _validate_gate. Retire input immediately there too.
            if kind=='identity' and self.executor.armed:
                try:self.executor._disarm('focus_or_calibration_invalid',source='identity_unavailable')
                except BaseException as exc:
                    with self.gate_observation_lock:
                        self.identity_recovery['release_error']=type(exc).__name__
                    raise
        return gate

    def report_identity_recovery(self):
        with self.gate_observation_lock:
            recovery=self.identity_recovery
            if recovery is None or recovery['reported']:return
            recovery['reported']=True
        self.event('grind_identity_recovery',{'status':'waiting',
            'observation':recovery['observation'],'window_key':list(recovery['window_key']),
            'release_error':recovery.get('release_error'),
            'input_authorized':False,'deadline':self.controller.deadline})

    def input_reconciled(self, *, strict=False):
        cycle=self.controller.cycle
        if getattr(cycle,'_active_receipt',None) is not None:return False
        generation=0;last=None
        # Check the durable issuance ledger, including earlier receipts: a later
        # observation must never hide partial or unknown physical input.
        rows=self.store.connection.execute(
            "SELECT payload_json FROM events WHERE event_type='execution_receipt' ORDER BY rowid")
        for (payload,) in rows:
            receipt=json.loads(payload);steps=receipt.get('input_steps',[])
            if receipt.get('generation_before')!=generation or receipt.get('dispatch_unknown'):return False
            if receipt.get('possible_input'):
                if not (steps and receipt.get('completed') and not receipt.get('error')
                    and all(s.get('status')=='completed' for s in steps)):
                    fingerprint=hashlib.sha256(json.dumps(receipt,sort_keys=True).encode()).hexdigest()
                    if strict or self.reconciled_movements.get(receipt.get('receipt_id'))!=fingerprint:return False
            elif receipt.get('attempted') or receipt.get('dispatched') or steps:return False
            generation+=len(steps)
            if receipt.get('generation_after')!=generation:return False
            last=receipt
        return generation==cycle.input_generation and last==cycle.last_receipt

    def gate_recovery_current(self, *, armed=False):
        recovery=self.periodic_gate_recovery
        if not recovery or self.stopping or self.controller.stopped:return False
        reason=None
        if time.time()>=self.controller.deadline:reason='absolute_deadline'
        elif time.monotonic()>=recovery['deadline']:reason='idle_gate_recovery_timeout'
        elif (self.periodic_gate_context()!=recovery['context']
                or self.profile.values!=recovery['profile'] or self.controller.config!=recovery['config']
                or self.executor._gate_lease!=recovery['lease']+int(armed)
                or self.executor.armed!=armed
                or (not armed and self.executor.periodic_timeout_trip!=recovery['trip'])):
            reason='idle_gate_recovery_authority_changed'
        elif self.invalid_gate is not None:reason='focus_or_calibration_invalid'
        if reason:
            self.request_stop(reason);return False
        return True

    async def recover_idle_gate(self, frozen):
        """Retire only a proved idle expiry; never continue an interrupted action."""
        e=self.executor;c=self.controller;trip=e.periodic_timeout_trip
        idle=bool(isinstance(trip,dict) and trip.get('reason')=='periodic_gate_inspection_timeout'
            and 'execution_id' in trip and trip['execution_id'] is None
            and trip.get('execution_active') is False
            and trip.get('held_inputs')==[] and 'release_error' in trip and trip['release_error'] is None
            and trip.get('retired_lease')==e._gate_lease
            and trip.get('expired_lease')==trip.get('request_lease')
            and trip.get('context')==self.periodic_gate_context()
            and e.disarm_reason=='periodic_gate_inspection_timeout' and not e.armed
            and self.invalid_gate is None and self.periodic_gate_recovery is None)
        if not idle or self.periodic_gate_recoveries>=self.IDLE_GATE_RECOVERIES:
            self.event('grind_periodic_gate_recovery',{'status':'refused','trip':trip,
                'idle_at_expiry':idle,'episodes':self.periodic_gate_recoveries})
            self.request_stop('idle_gate_recovery_exhausted' if idle else 'periodic_gate_inspection_timeout');return
        self.periodic_gate_recoveries+=1
        recovery={'trip':deepcopy(trip),'deadline':trip['detected_at_monotonic']+self.IDLE_GATE_RECOVERY_SECONDS,
            'lease':trip['retired_lease']+1,
            'episode':self.periodic_gate_recoveries,'retired_session_epoch':c.cycle.session_epoch,
            'profile':deepcopy(self.profile.values),'config':deepcopy(c.config)}
        self.periodic_gate_recovery=recovery
        c.pause_focus('periodic_gate_inspection_timeout')
        recovery['context']=deepcopy(self.periodic_gate_context())
        self.state='PAUSED_OPERATOR' if self.operator_paused else 'PAUSED_GATE'
        self.reason='periodic_gate_inspection_timeout'
        pending=self.task
        if pending is not None and not pending.done():pending.cancel()
        # Keep both tasks reachable through terminal teardown if a cancellation
        # resistant provider or release/drain exceeds this recovery's budget.
        self.gate_retirement_task=asyncio.create_task(e.stop('idle_gate_recovery'))
        self.event('grind_periodic_gate_recovery',{'status':'retiring','trip':trip,
            'episode':recovery['episode'],'rearm_deadline_monotonic':recovery['deadline']})
        self.event('grind_paused',{'state':self.state,'reason':self.reason,
            'deadline':c.deadline,'input_generation':c.cycle.input_generation,
            'input_reconciled':False,'automatic_resume':not self.operator_paused,
            'recovery_cause':'periodic_gate_inspection_timeout'})
        await self.status()

        def control_and_time():
            command=self.control()
            if command=='stop':self.request_stop('operator_stop')
            elif command=='pause':self.operator_paused=True;self.state='PAUSED_OPERATOR'
            elif command=='resume':self.operator_paused=False;self.state='PAUSED_GATE'
            if self.stopping or c.stopped:return False
            if time.time()>=c.deadline:self.request_stop('absolute_deadline');return False
            if time.monotonic()>=recovery['deadline']:
                self.request_stop('idle_gate_recovery_timeout');return False
            if (self.periodic_gate_context()!=recovery['context'] or self.profile.values!=recovery['profile']
                    or c.config!=recovery['config']):
                self.request_stop('idle_gate_recovery_authority_changed');return False
            return True

        tasks={task for task in (pending,self.gate_retirement_task) if task is not None}
        while any(not task.done() for task in tasks):
            if not control_and_time():return
            await asyncio.wait(tasks,timeout=min(.1,max(0.,recovery['deadline']-time.monotonic())))
        if not control_and_time():return
        for task in tasks:
            if task.cancelled():
                if task is self.gate_retirement_task:
                    self.request_stop('input_release_failed');return
                continue
            error=task.exception()
            if error is not None:
                self.request_stop('idle_gate_recovery_task_failed:'+type(error).__name__);return
            if getattr(task.result(),'status',None)=='evidence_retention_failed':
                self.request_stop('evidence_retention_failed');return
        if self.task is pending:self.task=None
        self.gate_retirement_task=None
        if getattr(c.archive,'failed',None):self.request_stop('evidence_retention_failed');return
        if not self.input_reconciled(strict=True):self.request_stop('partial_or_unknown_grind_input');return
        while e.periodic_gate_busy or self.operator_paused:
            if not control_and_time():return
            await asyncio.sleep(.05)
        if not self.gate_recovery_current():return
        changed=await asyncio.to_thread(frozen.changed)
        if changed:self.request_stop(changed);return
        if not self.gate_recovery_current():return
        if shutil.disk_usage(self.directory).free<int(self.config.get('disk_reserve_bytes',1024**3)):
            self.request_stop('evidence_disk_reserve');return
        # Exactly one new periodic observation. A timeout here is terminal and
        # cannot spend another episode or replace the original expiry proof.
        try:gate=await self.poll_gate_state()
        except ExecutionRejected:
            self.request_stop(e.disarm_reason or 'periodic_gate_inspection_failed');return
        if gate!='valid':self.request_stop('focus_or_calibration_invalid');return
        if not self.gate_recovery_current():return
        self.recovery_probe_at=time.monotonic()
        self.event('grind_periodic_gate_recovery',{'status':'reconciled','episode':recovery['episode'],
            'input_generation':c.cycle.input_generation,'fresh_world_required':True})
        while control_and_time():
            await asyncio.sleep(.1)
            if not control_and_time() or not self.gate_recovery_current():return
            if self.operator_paused:continue
            changed=await asyncio.to_thread(frozen.changed)
            if changed:self.request_stop(changed);return
            if not self.gate_recovery_current():return
            if shutil.disk_usage(self.directory).free<int(self.config.get('disk_reserve_bytes',1024**3)):
                self.request_stop('evidence_disk_reserve');return
            if not self.input_reconciled(strict=True):self.request_stop('partial_or_unknown_grind_input');return
            if self.resume():return

    def reconcile_released_movement(self):
        """After cancellation/release, retain unknown movement and retire input."""
        if (not self.identity_recovery or self.executor.armed or self.controller.stopped
                or getattr(self.controller.cycle,'_active_receipt',None) is not None):return
        receipt=self.controller.cycle.last_receipt or {};rid=receipt.get('receipt_id')
        if not rid or rid in self.reconciled_movements or receipt.get('completed'):return
        self.drain_executor_timing()
        guard={};release={}
        for (raw,) in self.store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='dispatch_guard_checked' ORDER BY rowid"):
            item=json.loads(raw)
            if item.get('request_id')==receipt.get('request_id'):guard=item
        for (raw,) in self.store.connection.execute(
                "SELECT payload_json FROM events WHERE event_type='input_release_timing' ORDER BY rowid"):
            item=json.loads(raw)
            if item.get('execution_id')==rid and item.get('source')=='identity_unavailable':release=item
        from sage_wow.agent.grind_interruption import released_movement
        action=released_movement(self.profile,receipt,self.identity_recovery,guard,release)
        if action is None:return
        fingerprint=hashlib.sha256(json.dumps(receipt,sort_keys=True).encode()).hexdigest()
        self.reconciled_movements[rid]=fingerprint
        # A released final key cannot cover up an earlier ledger gap.
        if not self.input_reconciled():
            del self.reconciled_movements[rid];return
        hunt=self.controller.hunt
        record={'status':'unknown_unassessed','reason':'released_identity_interrupted_movement',
            'action':action,'family':'motion','purpose':self.controller.world_resume_phase,
            'receipt':receipt,'source_frame_id':receipt['source_frame_id']}
        hunt.unassessed_count+=1;hunt.unassessed=(hunt.unassessed+[record])[-24:]
        hunt.last_gameplay_receipt_id=rid;hunt.retry_credit=False
        hunt.failed_action(record)
        if self.controller.world_resume_phase!='travel':
            debt=hunt.motion_key(action,'search')
            hunt.unresolved_motion[debt]=hunt.unresolved_motion.get(debt,0)+1
        self.event('grind_interrupted_movement_reconciled',{'receipt_id':rid,
            'receipt_sha256':fingerprint,'action':action,'release':release,
            'guard_request_id':guard['request_id'],'gameplay_outcome':'unknown',
            'receipt_completed':False,'fresh_world_required':True,'automatic_replay':False})

    async def pause(self,*,operator=False):
        self.operator_paused=self.operator_paused or operator
        if self.state in PAUSED_STATES:
            if self.operator_paused:self.state='PAUSED_OPERATOR'
            return
        reason='operator_pause' if operator else ('application_identity_unavailable' if self.identity_recovery else 'clean_focus_lost')
        self.report_identity_recovery()
        if self.executor.disarm_reason=='controller_heartbeat_lost':
            trip=self.executor.last_watchdog_trip
            idle=bool(isinstance(trip,dict) and trip.get('reason')=='controller_heartbeat_lost'
                and 'execution_id' in trip and trip['execution_id'] is None
                and trip.get('held_inputs')==[] and 'release_error' in trip and trip['release_error'] is None)
            # Retain the expired lease before stop/arm changes executor state.
            # No held keys alone does not prove a click/text/chord was complete.
            self.drain_executor_timing()
            self.event('grind_heartbeat_recovery',{'status':'retiring' if idle else 'refused',
                'reason':'controller_heartbeat_lost','idle_at_expiry':idle,'trip':trip,
                'session_epoch':self.controller.cycle.session_epoch})
            if not idle:
                self.request_stop('controller_heartbeat_lost' if self.input_reconciled()
                    else 'partial_or_unknown_grind_input');return
            self.heartbeat_recovery={'trip':dict(trip),'retired_session_epoch':self.controller.cycle.session_epoch}
            reason='controller_heartbeat_lost'
        known_movement=(self.controller.cycle.last_receipt or {}).get('receipt_id') in self.reconciled_movements
        self.controller.pause_focus(reason,defer_input_reconciliation=self.identity_recovery is not None or known_movement)
        pending=self.task;self.task=None
        if pending is not None and not pending.done():pending.cancel()
        await self.executor.stop(reason)
        if pending is not None:
            outcome=(await asyncio.gather(pending,return_exceptions=True))[0]
            from sage_wow.platform.macos.capture import ForegroundCaptureInterrupted
            if isinstance(outcome,Exception) and not isinstance(outcome,ForegroundCaptureInterrupted):raise outcome
            if getattr(outcome,'status',None)=='evidence_retention_failed':
                self.request_stop('evidence_retention_failed');return
        if getattr(self.controller.archive,'failed',None):
            self.request_stop('evidence_retention_failed');return
        if self.identity_recovery and self.identity_recovery.get('release_error'):
            self.request_stop('input_release_failed');return
        if self.gate_state()=='invalid':
            self.request_stop('focus_or_calibration_invalid',source='pause_gate_recheck');return
        self.reconcile_released_movement()
        if not self.input_reconciled():
            self.request_stop('partial_or_unknown_grind_input');return
        if self.controller.stopped:return
        self.state='PAUSED_OPERATOR' if self.operator_paused else ('PAUSED_HEARTBEAT' if self.heartbeat_recovery
            else ('PAUSED_IDENTITY' if self.identity_recovery else 'PAUSED_FOCUS'))
        self.reason='operator_pause' if self.operator_paused else reason
        if self.heartbeat_recovery:self.recovery_probe_at=time.monotonic()
        self.event('grind_paused',{'state':self.state,'reason':self.reason,
            'deadline':self.controller.deadline,'input_generation':self.controller.cycle.input_generation,
            'input_reconciled':True,'automatic_resume':not self.operator_paused,
            'recovery_cause':'controller_heartbeat_lost' if self.heartbeat_recovery
                else ('application_identity_unavailable' if self.identity_recovery else None)})
        await self.status()

    def resume(self):
        if self.stopping or self.controller.stopped:return False
        if self.periodic_gate_recovery and not self.gate_recovery_current():return False
        if self.executor.periodic_gate_busy:return False
        if time.time()>=self.controller.deadline:
            self.request_stop('absolute_deadline');return False
        if self.identity_recovery and self.identity_recovery.get('release_error'):
            self.request_stop('input_release_failed');return False
        if self.invalid_gate is not None and gate_recovery_kind(self.invalid_gate)=='invalid':
            self.request_stop('focus_or_calibration_invalid',source='resume_latched_gate');return False
        if self.operator_paused or self.gate_state()!='valid':return False
        if self.periodic_gate_recovery and not self.gate_recovery_current():return False
        if not self.input_reconciled(strict=bool(self.periodic_gate_recovery)):
            self.request_stop('partial_or_unknown_grind_input');return False
        if self.heartbeat_recovery or self.periodic_gate_recovery:
            # Stay disarmed for a responsive runner turn. A delayed turn starts
            # this probe again; elapsed time never renews the expired lease.
            elapsed=time.monotonic()-self.recovery_probe_at
            if elapsed>self.executor.heartbeat_timeout:
                self.recovery_probe_at=time.monotonic();return False
            if elapsed<.1:return False
        inspect=self.executor.inspect_gate;arm_gate=[]
        with self.gate_observation_lock:sequence=self.gate_observation_sequence
        def record_gate():
            gate=inspect();arm_gate.append(gate);return gate
        self.executor.inspect_gate=record_gate
        try:self.executor.arm()
        except ExecutionRejected:
            if not arm_gate or gate_recovery_kind(arm_gate[-1])=='invalid':raise
            return False
        finally:self.executor.inspect_gate=inspect
        if self.periodic_gate_recovery and not self.gate_recovery_current(armed=True):
            self.executor._disarm('idle_gate_recovery_authority_changed');return False
        if self.periodic_gate_recovery and not self.input_reconciled(strict=True):
            self.executor._disarm('partial_or_unknown_grind_input')
            self.request_stop('partial_or_unknown_grind_input');return False
        with self.gate_observation_lock:
            interrupted=self.gate_observation_sequence!=sequence or not self.executor.armed
            if not interrupted:
                self.invalid_gate=None;self.invalid_gate_observation=None
                identity_recovery=self.identity_recovery;self.identity_recovery=None
        if interrupted:
            self.executor._disarm('focus_or_calibration_invalid',source='resume_gate_changed')
            return False
        self.controller.resume_focus()
        self.reason=None;self.state='RESUMING'
        if identity_recovery:
            self.event('grind_identity_recovery',{'status':'verified',
                'observation':identity_recovery['observation'],'window_key':list(identity_recovery['window_key']),
                'verified_gate':asdict(arm_gate[-1]),'fresh_world_required':True,
                'session_epoch':self.controller.cycle.session_epoch,'deadline':self.controller.deadline})
        if self.heartbeat_recovery:
            self.event('grind_heartbeat_recovery',{'status':'new_lease','reason':'controller_heartbeat_lost',
                **self.heartbeat_recovery,'session_epoch':self.controller.cycle.session_epoch,
                'input_generation':self.controller.cycle.input_generation,
                'deadline':self.controller.deadline,'fresh_world_required':True})
            self.heartbeat_recovery=None;self.recovery_probe_at=None
        if self.periodic_gate_recovery:
            recovery=self.periodic_gate_recovery
            self.event('grind_periodic_gate_recovery',{'status':'new_lease','episode':recovery['episode'],
                'retired_session_epoch':recovery['retired_session_epoch'],
                'session_epoch':self.controller.cycle.session_epoch,'deadline':self.controller.deadline,
                'fresh_world_required':True})
            self.periodic_gate_recovery=None;self.recovery_probe_at=None
        self.event('grind_resuming',{'session_id':self.config['session_id'],
            'deadline':self.controller.deadline,'fresh_world_required':True,
            'next_level_check_at':None if self.controller.level.last_attempt_at is None
                else self.controller.level.last_attempt_at+300})
        return True

    async def run(self):
        if any((self.directory/name).exists() for name in ('events.sqlite3','status.json','launch-manifest.json','trial-evidence')):
            raise ValueError('Finished/existing trial directory is immutable; use a fresh directory')
        self.directory.mkdir(parents=True,exist_ok=True)
        lock=(self.directory/'runner.lock').open('a+')
        locked=False;hotkey=None
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);locked=True
            if any((self.directory/name).exists() for name in ('events.sqlite3','status.json','launch-manifest.json','trial-evidence')):
                raise ValueError('Existing trial directory is immutable; use a fresh directory')
            self.store=EventStore(self.directory/'events.sqlite3')
            if self.store.connection.execute('SELECT count(*) FROM events').fetchone()[0]:
                raise ValueError('Grind run requires a fresh event directory; no checkpoints are imported')
            frozen=self.freeze_factory(self.profile,self.directory)
            self.event('launch_attempt_frozen',frozen.manifest)
            self.live_dependencies()
            self.observe_executor_gate()
            self.capture=self.capture or (lambda:capture_window(self.profile.window_id,self.directory/'captures'))
            max_bytes=int(self.config.get('evidence_max_bytes',8*1024**3))
            reserve=int(self.config.get('disk_reserve_bytes',1024**3))
            if max_bytes<=0 or reserve<1024**3 or shutil.disk_usage(self.directory).free<max_bytes+reserve:
                raise ValueError('Grinding evidence budget plus disk reserve unavailable')
            archive=EvidenceArchive(self.directory/'trial-evidence',self.store,max_bytes=max_bytes)
            if self.ocr is None:
                from sage_wow.perception.ocr_process import IsolatedOCR
                self.ocr_worker=IsolatedOCR()
                self.ocr=self.ocr_worker
            kwargs={'ocr':self.ocr}
            self.controller=GrindController(self.profile,self.store,self.sage,self.executor,capture=self.capture,archive=archive,**kwargs)
            self.sage.image_evidence_sink=archive.wire_image
            try:self.executor.arm()
            except ExecutionRejected:
                if self.identity_recovery is None or self.gate_state()=='invalid':raise
                await self.pause()
            self.pulse=asyncio.create_task(self.heartbeat())
            if self.state not in PAUSED_STATES:self.state='PLAYING'
            await self.status()
            self.event('session_started',{'mode':'grind_only','session_id':self.config['session_id'],
                'session_epoch':self.controller.cycle.session_epoch,'deadline':self.controller.deadline,
                'quest_and_services_enabled':False,'loot_enabled':self.config['loot_enabled'],
                'smite_burst_count':self.config['smite_burst_count'],
                'heal_health_fraction':self.config['heal_health_fraction']})
            if not any(os.getenv(name)=='1' for name in ('SAGE_WOW_DISABLE_LIVE_INPUT','SAGE_WOW_DISABLE_LIVE_CAPTURE')):
                from sage_wow.dashboard.session_source import write_session_source
                write_session_source(self.directory,self.profile.window_id,self.config['session_id'])
                from sage_wow.platform.macos.hotkey import GlobalStopHotkey
                loop=asyncio.get_running_loop()
                hotkey=GlobalStopHotkey(self.profile.values.get('controls',{}).get('stop_hotkey','ctrl+alt+escape'),
                    lambda:loop.call_soon_threadsafe(self.request_stop,'operator_global_stop'))
                if not await asyncio.to_thread(hotkey.start):self.event('grind_stop_hotkey_unavailable',{'control_file_and_signals_available':True})
            checked=0.
            while not self.stopping and not self.controller.stopped:
                command=self.control()
                if command=='stop':self.request_stop('operator_stop');break
                if command=='pause':await self.pause(operator=True)
                elif command=='resume':self.operator_paused=False
                if time.time()>=self.controller.deadline:
                    unresolved=self.controller.hunt.blocked
                    self.request_stop('absolute_deadline:'+unresolved['reason'] if unresolved else 'absolute_deadline');break
                if time.monotonic()-checked>=2:
                    changed=await asyncio.to_thread(frozen.changed);checked=time.monotonic()
                    if changed:self.request_stop(changed);break
                if shutil.disk_usage(self.directory).free<int(self.config.get('disk_reserve_bytes',1024**3)):
                    self.request_stop('evidence_disk_reserve');break
                if not self.executor.armed and self.executor.disarm_reason=='periodic_gate_inspection_timeout':
                    await self.recover_idle_gate(frozen);continue
                try:gate=await self.poll_gate_state()
                except ExecutionRejected:
                    if not self.executor.armed and self.executor.disarm_reason=='periodic_gate_inspection_timeout':
                        await self.recover_idle_gate(frozen);continue
                    raise
                if gate=='retired':
                    await asyncio.sleep(.1);continue
                self.report_identity_recovery()
                if gate=='invalid':
                    source='executor_disarmed_gate_check' if not self.executor.armed and self.executor.disarm_reason=='focus_or_calibration_invalid' else 'runner_gate_check'
                    self.request_stop('focus_or_calibration_invalid',source=source);break
                paused=self.state in PAUSED_STATES
                if not paused and not self.executor.armed:
                    if self.executor.disarm_reason=='controller_heartbeat_lost':
                        await self.pause()
                    elif self.executor.disarm_reason!='focus_or_calibration_invalid':
                        self.request_stop(self.executor.disarm_reason or 'executor_disarmed');break
                    elif self.invalid_gate is None or gate_recovery_kind(self.invalid_gate)=='invalid':
                        self.request_stop('focus_or_calibration_invalid',source='executor_disarmed_gate_check');break
                    else:
                        # Watchdog can observe a brief focus loss already restored
                        # by this check. Retire all interrupted authority anyway.
                        await self.pause()
                    paused=True
                if not paused and (gate in {'focus','identity'} or self.identity_recovery is not None):
                    await self.pause();paused=True
                if self.stopping or self.controller.stopped:break
                if paused:
                    if gate=='valid' and not self.operator_paused:
                        changed=await asyncio.to_thread(frozen.changed)
                        if changed:self.request_stop(changed);break
                        if time.time()>=self.controller.deadline:
                            unresolved=self.controller.hunt.blocked
                            self.request_stop('absolute_deadline:'+unresolved['reason'] if unresolved else 'absolute_deadline');break
                        self.resume()
                    await self.status();await asyncio.sleep(.1);continue
                if self.task is not None and self.task.done():
                    from sage_wow.platform.macos.capture import ForegroundCaptureInterrupted
                    try:result=await self.task
                    except ForegroundCaptureInterrupted:
                        await self.pause()
                        continue
                    self.task=None
                    self.event('grind_cycle_result',{'status':result.status,'detail':result.detail,
                        'choice':getattr(result.decision,'chosen',None) or (result.receipt or {}).get('chosen_option'),
                        'authorization_type':(result.receipt or {}).get('authorization_type'),
                        'progress_credit':False})
                    if result.status=='evidence_retention_failed':self.request_stop(result.status);break
                    if self.controller.stopped:break
                    if self.state=='RESUMING' and not self.controller.require_world:
                        self.state='PLAYING'
                        self.event('grind_resumed',{'session_id':self.config['session_id'],
                            'deadline':self.controller.deadline,'fresh_world_verified':True,'retained_level':self.controller.level.last_confirmed_level})
                    captures=self.directory/'captures'
                    if captures.exists():
                        for path in captures.glob('frame-*.jpg'):path.unlink()
                if self.task is None and time.time()>=self.controller.wait_until:
                    self.task=asyncio.create_task(self.step(),name='grind-decision-cycle')
                await self.status();await asyncio.sleep(.1)
            self.reason=self.reason or self.controller.reason or 'absolute_deadline'
        except asyncio.CancelledError:
            self.request_stop('runner_cancelled');raise
        except Exception as exc:
            self.request_stop(type(exc).__name__+': '+str(exc)[:250])
            if self.store:self.event('grind_failed',{'reason':self.reason})
            raise
        finally:
            self.stopping=True
            if self.controller and not self.controller.stopped:self.controller.stop(self.reason or 'runner_finalized')
            tasks=[task for task in (self.task,self.pulse,self.gate_retirement_task) if task is not None]
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            try:
                try:
                    if hotkey:hotkey.stop()
                finally:
                    if self.executor:
                        try:await self.executor.stop(self.reason or 'grind_finished')
                        finally:
                            if self.store:self.drain_executor_timing()
                        if self.original_gate_inspector is not None:
                            self.executor.inspect_gate=self.original_gate_inspector
                            self.executor.configure_periodic_gate()
            finally:
                try:
                    backend=getattr(self.executor,'backend',None)
                    if backend and callable(getattr(backend,'close',None)):backend.close()
                finally:
                    try:
                        try:
                            if self.ocr_worker:await asyncio.to_thread(self.ocr_worker.close)
                        finally:
                            if self.sage and callable(getattr(self.sage,'close',None)):await self.sage.close()
                    finally:
                        self.state='STOPPED'
                        try:
                            if self.store:
                                try:
                                    self.event('session_stopped',{'mode':'grind_only','reason':self.reason,
                                        'success':bool(self.controller and self.controller.success),
                                        'gate_failure':self.gate_failure})
                                    await self.status()
                                finally:self.store.close()
                        finally:
                            try:
                                if locked:fcntl.flock(lock,fcntl.LOCK_UN)
                            finally:lock.close()


async def launch(profile,directory):
    runner=GrindRunner(profile,directory);loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        loop.add_signal_handler(sig,runner.request_stop,'operator_signal')
    await runner.run()


def main():
    from dotenv import load_dotenv
    load_dotenv()
    parser=argparse.ArgumentParser(description='Fresh isolated mob-grind run; no quest/services/task graph')
    parser.add_argument('--profile',type=Path,required=True);parser.add_argument('--data-dir',type=Path,required=True)
    args=parser.parse_args();asyncio.run(launch(load_profile(args.profile),args.data_dir))


if __name__=='__main__':main()
