"""Click-through, nonactivating native HUD; never a game input owner."""
import fcntl
from pathlib import Path
import signal
from sage_wow.dashboard.telemetry import display_lines
from sage_wow.dashboard.session_source import overlay_snapshot, pointer_path


def run_overlay(directory: Path, window_id: int, x=24, y=48, *, follow=True):
    import AppKit as A
    import Foundation as F
    import Quartz as Q
    directory.mkdir(parents=True,exist_ok=True)
    lock_path = pointer_path().parent/'live-overlay.lock' if follow else directory/'overlay.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock=lock_path.open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return
    app=A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
    class HUDPanel(A.NSPanel):
        def canBecomeKeyWindow(self):return False
        def canBecomeMainWindow(self):return False
    panel=HUDPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        A.NSMakeRect(x,200,455,210),A.NSWindowStyleMaskBorderless|A.NSWindowStyleMaskNonactivatingPanel,A.NSBackingStoreBuffered,False)
    panel.setTitle_('Sage Live Decisions')
    panel.setLevel_(A.NSFloatingWindowLevel)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setIgnoresMouseEvents_(True)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(.025,.04,.065,.92))
    panel.setHasShadow_(True)
    panel.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces|A.NSWindowCollectionBehaviorFullScreenAuxiliary)
    labels=[]
    for i in range(8):
        label=A.NSTextField.labelWithString_('')
        label.setFrame_(A.NSMakeRect(14,181-i*24,430,22))
        label.setFont_(A.NSFont.monospacedSystemFontOfSize_weight_(12 if i else 13,A.NSFontWeightMedium))
        label.setTextColor_(A.NSColor.whiteColor() if i in (0,2) else A.NSColor.colorWithCalibratedWhite_alpha_(.78,1))
        panel.contentView().addSubview_(label);labels.append(label)
    class Poller(F.NSObject):
        def tick_(self,timer):
            try:
                data, selected_window = overlay_snapshot(directory, window_id, follow=follow)
                identity=(data['session_source'], selected_window, data['state'])
                if identity != getattr(self, 'last_source', None):
                    print(f'HUD source={identity[0]} window={identity[1]} state={identity[2]}', flush=True)
                    self.last_source=identity
                lines=display_lines(data)
                lines[7]='Source: '+data['session_source']
                for label,text in zip(labels,lines):label.setStringValue_(text)
                labels[2].setTextColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(.3,.9,.75,1))
                labels[6].setTextColor_(A.NSColor.systemOrangeColor() if (data['progress_age'] or 0)>180 else A.NSColor.whiteColor())
                rows=Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly,Q.kCGNullWindowID) or []
                game=next((r for r in rows if int(r.get(Q.kCGWindowNumber,0))==selected_window),None)
                front=next((r for r in rows if int(r.get(Q.kCGWindowLayer,-1))==0),None)
                if game and front and int(front.get(Q.kCGWindowNumber,0))==selected_window:
                    b=game[Q.kCGWindowBounds];primary=A.NSScreen.screens()[0].frame().size.height
                    panel.setFrameOrigin_(A.NSMakePoint(float(b['X'])+x,primary-float(b['Y'])-y-210))
                    panel.orderFrontRegardless()
                else:panel.orderOut_(None)
            except Exception as exc:
                for label in labels:label.setStringValue_('')
                labels[0].setStringValue_('SAGE · telemetry temporarily unavailable')
                labels[4].setStringValue_(type(exc).__name__)
    poller=Poller.alloc().init()
    timer=F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(.25,poller,'tick:',None,True)
    signal.signal(signal.SIGTERM,lambda *_: app.terminate_(None))
    signal.signal(signal.SIGINT,lambda *_: app.terminate_(None))
    poller.tick_(timer)
    app.run()
