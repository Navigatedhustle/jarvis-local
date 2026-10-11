"""Opt-in observation of the actual launcher microphone; no raw audio is saved."""

import json
import time


class VoiceBrowserProbe:
    def __init__(self, page, folder):
        self.page, self.folder = page, folder
        self.frames = self.bytes = 0
        self.events = []
        self.errors = []
        self.last = 0
        self.device_levels = None
        page.on("pageerror", lambda error: self.errors.append(type(error).__name__))
        page.on("websocket", self.socket)

    def socket(self, socket):
        if not socket.url.endswith("/api/voice"):
            return

        def sent(data):
            if isinstance(data, bytes):
                self.frames += 1
                self.bytes += len(data)

        def received(data):
            if isinstance(data, str):
                event = json.loads(data)
                self.events.append(
                    {k: event[k] for k in ("type", "state", "turn_id", "timings") if k in event}
                )

        socket.on("framesent", sent)
        socket.on("framereceived", received)

    def start(self):
        self.page.locator("#voice-button").click()

    def sample(self):
        if time.monotonic() - self.last < 2:
            return
        self.last = time.monotonic()
        state = self.page.evaluate("""async () => {
          let permission = 'unavailable';
          try { permission = (await navigator.permissions.query({name:'microphone'})).state; } catch {}
          const devices = navigator.mediaDevices ? await navigator.mediaDevices.enumerateDevices() : [];
          let rms = 0;
          if (mic && audioContext) {
            if (!window.probeAnalyser || window.probeContext !== audioContext) {
              window.probeContext = audioContext;
              window.probeAnalyser = audioContext.createAnalyser();
              audioContext.createMediaStreamSource(mic).connect(window.probeAnalyser);
            }
            const samples = new Float32Array(window.probeAnalyser.fftSize);
            window.probeAnalyser.getFloatTimeDomainData(samples);
            rms = Math.sqrt(samples.reduce((sum,v)=>sum+v*v,0)/samples.length);
          }
          return {secure_context:isSecureContext, permission,
            devices:devices.filter(d=>d.kind==='audioinput').map(d=>({name:d.label,default:d.deviceId==='default'})),
            tracks:mic?.getAudioTracks().map(t=>({name:t.label,enabled:t.enabled,muted:t.muted,state:t.readyState,settings:t.getSettings()})) || [],
            stream_active:mic?.active || false, rms, context_state:audioContext?.state,
            context_rate:audioContext?.sampleRate, websocket:voiceSocket?.readyState,
            state:document.querySelector('.voice-panel')?.dataset.state,
            help:document.querySelector('.voice-panel small')?.textContent};
        }""")
        for track in state["tracks"]:
            track["settings"].pop("deviceId", None)
            track["settings"].pop("groupId", None)
        if state["permission"] == "granted" and state["stream_active"] and self.device_levels is None:
            self.device_levels = self.page.evaluate("""async () => {
              const devices=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='audioinput' && !['default','communications'].includes(d.deviceId));
              const result=[];
              for (const device of devices) {
                let stream,context;
                try {
                  stream=await navigator.mediaDevices.getUserMedia({audio:{deviceId:{exact:device.deviceId},echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
                  context=new AudioContext(); await context.resume();
                  const analyser=context.createAnalyser(); context.createMediaStreamSource(stream).connect(analyser);
                  let peak=0; const end=performance.now()+1200;
                  while (performance.now()<end) {
                    const data=new Float32Array(analyser.fftSize); analyser.getFloatTimeDomainData(data);
                    peak=Math.max(peak,Math.sqrt(data.reduce((sum,v)=>sum+v*v,0)/data.length));
                    await new Promise(resolve=>setTimeout(resolve,50));
                  }
                  result.push({device:device.label,peak_rms:peak,real_hardware:true});
                } catch(error) {result.push({device:device.label,error:error.name});}
                finally {stream?.getTracks().forEach(t=>t.stop()); if(context) await context.close();}
              }
              return result;
            }""")
            usable = [item for item in self.device_levels if item.get("peak_rms", 0) > 0.002]
            if state["rms"] == 0 and len(usable) == 1:
                self.page.get_by_label("Microphone", exact=True).select_option(label=usable[0]["device"])
        self.folder.joinpath("real-microphone-probe.json").write_text(
            json.dumps(
                {
                    **state,
                    "frames": self.frames,
                    "bytes": self.bytes,
                    "events": self.events[-30:],
                    "errors": self.errors[-10:],
                    "device_level_checks": self.device_levels,
                },
                indent=2,
            )
        )
