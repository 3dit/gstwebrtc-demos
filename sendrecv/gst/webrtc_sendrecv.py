import random
import ssl
import time
import threading
import websockets
import asyncio
import os
import sys
import json
import argparse

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp
gi.require_version('GstVideo', '1.0')
from gi.repository import GstVideo

PIPELINE_DESC = '''
webrtcbin name=sendrecv bundle-policy=max-bundle stun-server=stun://stun.l.google.com:19302
 videotestsrc is-live=true pattern=ball ! videoconvert ! capsfilter caps=video/x-raw,format=NV12 !
 queue ! v4l2h264enc extra-controls="encode,repeat_sequence_header=1" !
 h264parse config-interval=-1 ! capsfilter caps=video/x-h264,profile=baseline,level=3.1,stream-format=byte-stream,alignment=au !
 rtph264pay pt=97 config-interval=-1 !
 queue ! application/x-rtp,media=video,encoding-name=H264,payload=97,clock-rate=90000,packetization-mode=1 ! sendrecv.
 audiotestsrc is-live=true wave=red-noise ! audioconvert ! audioresample ! queue ! opusenc ! rtpopuspay !
 queue ! application/x-rtp,media=audio,encoding-name=OPUS,payload=96 ! sendrecv.
'''

from websockets.version import version as wsv

class WebRTCClient:
    def __init__(self, id_, peer_id, server):
        self.id_ = id_
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.peer_id = peer_id
        self.server = server or 'wss://webrtc.nirbheek.in:8443'
        self.negotiation_started = False


    async def connect(self):
        sslarg = None
        if self.server.startswith("wss://"):
            sslctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
            sslctx.check_hostname = False
            sslctx.verify_mode = ssl.CERT_NONE
            sslarg = sslctx

        self.conn = await websockets.connect(self.server, ssl=sslarg)

        await self.conn.send('HELLO %d' % self.id_)

    async def setup_call(self):
        await self.conn.send('SESSION {}'.format(self.peer_id))

    def send_sdp_offer(self, offer):
        text = offer.sdp.as_text()
        print ('Sending offer:\n%s' % text)
        msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(msg))
        loop.close()

    def on_offer_created(self, promise, _, __):
        promise.wait()

        # reply = promise.get_reply()
        # offer = reply['offer']
        # promise.get_reply() returns a Gst.Structure on newer gi/GStreamer builds
        reply = promise.get_reply()
        offer = None
        try:
            offer = reply['offer']  # older bindings
        except TypeError:
            offer = reply.get_value('offer')  # newer bindings

        if offer is None:
            raise RuntimeError("Failed to get 'offer' from promise reply")

        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def on_negotiation_needed(self, element):
        if self.negotiation_started:
            return
        self.negotiation_started = True
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    def on_ice_state_changed(self, element, _):
        state = element.get_property('ice-connection-state')
        print('ICE connection state:', state.value_nick)
        if state.value_nick == 'failed':
            print('ICE failed — will close and retry...')
            # Close the websocket from a separate thread so the async
            # message loop sees ConnectionClosed and triggers a clean
            # reconnect cycle.
            if self.conn:
                close_loop = asyncio.new_event_loop()
                try:
                    close_loop.run_until_complete(self.conn.close())
                except Exception:
                    pass
                close_loop.close()

    def on_connection_state_changed(self, element, _):
        state = element.get_property('connection-state')
        print('Peer connection state:', state.value_nick)
        # Force a keyframe when the full connection (ICE + DTLS) is ready.
        # The initial keyframe was sent before DTLS completed and was dropped,
        # so the browser decoder can never start without a fresh one.
        if state.value_nick == 'connected':
            print('DTLS connected — forcing keyframe...')
            event = GstVideo.video_event_new_upstream_force_key_unit(
                Gst.CLOCK_TIME_NONE, True, 0)
            # Force-key-unit is an upstream event.  Sending it on the pipeline
            # routes it to sinks (downstream), which doesn't reach the encoder.
            # Instead, find the encoder's sink pad and push it there so it
            # travels upstream through the pipeline to the video encoder.
            sent = False
            for factory_name in ('x264enc', 'v4l2h264enc', 'vp8enc'):
                it = self.pipe.iterate_elements()
                while True:
                    ret, elem = it.next()
                    if ret != Gst.IteratorResult.OK:
                        break
                    f = elem.get_factory()
                    if f and f.get_name() == factory_name:
                        # send_event on the element itself routes upstream
                        # events correctly through the element.
                        if elem.send_event(event):
                            print(f'Force-key-unit sent to {elem.get_name()}')
                            sent = True
                            break
                if sent:
                    break
            if not sent:
                # Fallback: send on pipeline (may not work but worth trying)
                if self.pipe.send_event(event):
                    print('Force-key-unit event sent via pipeline')
                else:
                    print('Warning: Force-key-unit event failed')

    def on_signaling_state_changed(self, element, _):
        state = element.get_property('signaling-state')
        print('Signaling state:', state.value_nick)

    def _on_deep_element_added(self, _, __, element):
        """Called when an element is added deep inside webrtcbin's sub-bins."""
        factory = element.get_factory()
        if factory and factory.get_name() == 'rtpsession':
            try:
                element.set_property('rtcp-sync-send-time', False)
                print(f'Set rtcp-sync-send-time=false on {element.get_name()}')
            except Exception as e:
                print(f'Warning: could not set rtcp-sync-send-time: {e}')

    # ---- RTP flow diagnostics ----
    rtp_video_buffers = 0
    rtp_video_bytes = 0
    rtp_audio_buffers = 0
    _stats_timer = None

    @staticmethod
    def _video_probe_cb(pad, info, self_ref):
        buf = info.get_buffer()
        if buf:
            self_ref.rtp_video_buffers += 1
            self_ref.rtp_video_bytes += buf.get_size()
        return Gst.PadProbeReturn.OK

    @staticmethod
    def _audio_probe_cb(pad, info, self_ref):
        buf = info.get_buffer()
        if buf:
            self_ref.rtp_audio_buffers += 1
        return Gst.PadProbeReturn.OK

    def _print_rtp_stats(self):
        if self.pipe is None:
            return
        print(f'[RTP stats] video: {self.rtp_video_buffers} pkts '
              f'({self.rtp_video_bytes} bytes) | '
              f'audio: {self.rtp_audio_buffers} pkts')
        self._stats_timer = threading.Timer(5.0, self._print_rtp_stats)
        self._stats_timer.daemon = True
        self._stats_timer.start()

    def on_bus_error(self, _, msg):
        err, debug = msg.parse_error()
        print('GStreamer error:', err, debug)

    def on_bus_warning(self, _, msg):
        err, debug = msg.parse_warning()
        print('GStreamer warning:', err, debug)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        # Filter out empty candidate (end-of-candidates signal).
        # Sending it prematurely causes the browser to stop waiting for
        # STUN srflx candidates, which leads to ICE failure on reconnect.
        if not candidate:
            print('Suppressing empty end-of-candidates signal')
            return
        print('Sending ICE candidate', mlineindex, candidate)
        icemsg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(icemsg))
        loop.close()

    def on_incoming_decodebin_stream(self, _, pad):
        if not pad.has_current_caps():
            print (pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        if caps is None or caps.get_size() == 0:
            print(pad, 'has empty caps, ignoring')
            return
        s = caps.get_structure(0)
        name = s.get_name()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(resample)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)

    def start_pipeline(self):
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.webrtc.connect('notify::ice-connection-state', self.on_ice_state_changed)
        self.webrtc.connect('notify::connection-state', self.on_connection_state_changed)
        self.webrtc.connect('notify::signaling-state', self.on_signaling_state_changed)
        # Catch internal elements as they are added to webrtcbin's sub-bins
        # so we can set rtcp-sync-send-time=false on rtpsession (avoids the
        # "running time not set" SR error caused by TransportSendBin dropping
        # the pipeline latency event).
        self.webrtc.connect('deep-element-added', self._on_deep_element_added)
        # Set codec preferences on the video transceiver so the SDP offer
        # does not contain a fixed profile-level-id that conflicts with the
        # actual encoder output.
        try:
            trans = self.webrtc.emit('get-transceiver', 0)
            if trans:
                # Do NOT include profile-level-id here — it forces x264enc to
                # negotiate that level, which fails when the resolution exceeds
                # what the level allows (e.g. level 2.1 caps at 352×288).
                # Let x264enc pick the correct level for the actual resolution.
                caps = Gst.caps_from_string(
                    'application/x-rtp,media=video,encoding-name=H264,payload=97,'
                    'clock-rate=90000,packetization-mode=(string)1,'
                    'level-asymmetry-allowed=(string)1'
                )
                trans.set_property('codec-preferences', caps)
                print('Set H264 codec-preferences (packetization-mode=1, no fixed level)')
        except Exception as e:
            print(f'Warning: could not set video codec-preferences: {e}')
        # Set codec preferences on the audio transceiver so the OPUS SDP
        # stays correct across pipeline rebuilds (channels, fmtp, etc.).
        try:
            trans = self.webrtc.emit('get-transceiver', 1)
            if trans:
                caps = Gst.caps_from_string(
                    'application/x-rtp,media=audio,encoding-name=OPUS,payload=96,'
                    'clock-rate=48000,encoding-params=(string)2'
                )
                trans.set_property('codec-preferences', caps)
                print('Set OPUS codec-preferences on audio transceiver')
        except Exception as e:
            print(f'Warning: could not set audio codec-preferences: {e}')
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self.on_bus_error)
        bus.connect('message::warning', self.on_bus_warning)
        self.pipe.set_state(Gst.State.PLAYING)
        # Install pad probes to count RTP buffers entering webrtcbin
        self.rtp_video_buffers = 0
        self.rtp_video_bytes = 0
        self.rtp_audio_buffers = 0
        vpad = self.webrtc.get_static_pad('sink_0')
        if vpad:
            vpad.add_probe(Gst.PadProbeType.BUFFER, self._video_probe_cb, self)
            print(f'Installed video RTP probe on {vpad.get_name()}')
        else:
            print('Warning: could not find sink_0 on webrtcbin')
        apad = self.webrtc.get_static_pad('sink_1')
        if apad:
            apad.add_probe(Gst.PadProbeType.BUFFER, self._audio_probe_cb, self)
            print(f'Installed audio RTP probe on {apad.get_name()}')
        else:
            print('Warning: could not find sink_1 on webrtcbin')
        # Periodic stats
        self._print_rtp_stats()
        self.on_negotiation_needed(self.webrtc)

    def handle_sdp(self, message):
        assert (self.webrtc)
        msg = json.loads(message)
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert(sdp['type'] == 'answer')
            sdp = sdp['sdp']
            print ('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            print('Received ICE candidate', sdpmlineindex, candidate)
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)

    def close_pipeline(self):
        if self._stats_timer:
            self._stats_timer.cancel()
            self._stats_timer = None
        if self.pipe:
            bus = self.pipe.get_bus()
            if bus:
                bus.remove_signal_watch()
            self.pipe.set_state(Gst.State.NULL)
        self.pipe = None
        self.webrtc = None
        self.negotiation_started = False
        # Help GC release GStreamer element references so the next
        # pipeline rebuild starts with a clean slate.
        import gc
        gc.collect()

    async def loop(self):
        """Run the signalling message loop.  Returns 0 on clean
        disconnect (e.g. browser reload), 1 on fatal error."""
        assert self.conn
        try:
            async for message in self.conn:
                if message == 'HELLO':
                    await self.setup_call()
                elif message == 'SESSION_OK':
                    self.start_pipeline()
                elif message.startswith('ERROR'):
                    print(message)
                    self.close_pipeline()
                    return 1
                else:
                    self.handle_sdp(message)
        except websockets.ConnectionClosed:
            print('Websocket connection closed by server')
        except Exception as e:
            print(f'Unexpected error in message loop: {e}')
        self.close_pipeline()
        return 0

    async def stop(self):
        if self.conn:
            await self.conn.close()
        self.conn = None


def build_pipeline(args):
    video_queue = 'queue max-size-time=0 max-size-buffers=1 max-size-bytes=0 leaky=downstream'
    # The RTP queue before webrtcbin must hold all RTP packets for a whole
    # frame (at 800 kbps, ~7 packets).  max-size-buffers=1 causes the leaky
    # queue to drop most of them, breaking the stream for the browser.
    rtp_queue = 'queue max-size-time=200000000 max-size-buffers=60 max-size-bytes=0 leaky=downstream'
    audio_queue = 'queue max-size-time=0 max-size-buffers=2 max-size-bytes=0 leaky=downstream'

    if args.video_source == 'test':
        video_src = 'videotestsrc is-live=true pattern=ball'
        video_caps = ''
    elif args.video_source == 'libcamera':
        video_src = 'libcamerasrc'
        if args.width and args.height and args.framerate:
            video_caps = (
                f'video/x-raw,format=NV12,width={args.width},height={args.height},'
                f'framerate={args.framerate}/1'
            )
        else:
            video_caps = 'video/x-raw,format=NV12'
    elif args.video_source == 'v4l2':
        # io-mode=mmap avoids DMABuf caps that videoconvert can't handle
        video_src = f'v4l2src device={args.v4l2_device} io-mode=mmap'
        # Capture at native resolution, then use videorate to downsample fps
        # (many USB cameras only output 30fps regardless of caps request)
        src_caps_parts = []
        if args.width and args.height:
            src_caps_parts.append(f'width={args.width},height={args.height}')
        if src_caps_parts:
            video_caps_str = 'video/x-raw,' + ','.join(src_caps_parts)
        else:
            video_caps_str = 'video/x-raw'
        if args.framerate:
            video_caps = (
                f'{video_caps_str} ! videorate ! '
                f'video/x-raw,framerate={args.framerate}/1'
            )
        else:
            video_caps = video_caps_str
    else:
        raise ValueError(f"Unknown video source: {args.video_source}")

    rtp_mtu_prop = f' mtu={args.rtp_mtu}' if args.rtp_mtu else ''

    if args.video_codec == 'vp8':
        video_enc = f'vp8enc deadline=1 keyframe-max-dist={args.keyframe_interval}'
        video_pay = f'rtpvp8pay pt=97 picture-id-mode=1{rtp_mtu_prop}'
        video_rtp_caps = (
            'capsfilter caps=application/x-rtp,media=video,encoding-name=VP8,payload=97,clock-rate=90000'
        )
    elif args.video_codec == 'h264':
        is_v4l2 = args.h264_encoder.startswith('v4l2h264enc')
        enc_props = ''
        if args.bitrate:
            if is_v4l2 and not args.encoder_props:
                enc_props = (
                    f' extra-controls="encode,repeat_sequence_header=1,video_bitrate={args.bitrate * 1000}"'
                )
            else:
                enc_props = f' bitrate={args.bitrate}'
        if args.encoder_props:
            enc_props = (enc_props + ' ' + args.encoder_props).strip()
        video_enc = f'{args.h264_encoder}{(" " + enc_props) if enc_props else ""}'
        if is_v4l2:
            video_pay = (
                'h264parse config-interval=-1 ! '
                'capsfilter caps=video/x-h264,stream-format=byte-stream,alignment=au ! '
                f'rtph264pay pt=97 config-interval=-1{rtp_mtu_prop}'
            )
        else:
            # Force constrained-baseline profile for WebRTC browser compatibility
            video_pay = (
                'video/x-h264,profile=constrained-baseline ! '
                'h264parse config-interval=-1 ! '
                f'rtph264pay pt=97 config-interval=-1 aggregate-mode=zero-latency{rtp_mtu_prop}'
            )
        video_rtp_caps = ''
    else:
        raise ValueError(f"Unknown video codec: {args.video_codec}")

    src_caps_part = f' ! capsfilter caps={video_caps}' if video_caps else ''
    format_caps_part = ''
    if args.video_source == 'test':
        if args.video_codec == 'h264':
            if args.h264_encoder.startswith('v4l2h264enc'):
                format_caps_part = ' ! capsfilter caps=video/x-raw,format=NV12'
            else:
                format_caps_part = ' ! capsfilter caps=video/x-raw,format=I420'
        elif args.video_codec == 'vp8':
            format_caps_part = ' ! capsfilter caps=video/x-raw,format=I420'
    preview_chain = ''
    if args.preview:
        preview_chain = f'vtee. ! queue ! videoconvert ! autovideosink sync=false\n '

    if args.video_source == 'libcamera':
        if args.video_codec == 'h264' and not args.h264_encoder.startswith('v4l2h264enc'):
            fmt_caps = 'video/x-raw,format=I420'
        else:
            fmt_caps = 'video/x-raw,format=NV12'
        video_pre = (
            f'{video_src}{src_caps_part} ! queue ! videoconvert '
            f'! capsfilter caps={fmt_caps} '
        )
    elif args.video_source == 'v4l2':
        video_pre = (
            f'{video_src}{src_caps_part} ! queue ! videoconvert '
            f'! capsfilter caps=video/x-raw,format=I420 '
        )
    else:
        video_pre = f'{video_src}{src_caps_part} ! videoconvert{format_caps_part} '

    rtp_caps_part = f' ! {video_rtp_caps}' if video_rtp_caps else ''
    video_chain = (
        f'{video_pre}! tee name=vtee '
        f'vtee. ! {video_queue} ! {video_enc} ! {video_pay} ! {rtp_queue}{rtp_caps_part} ! sendrecv.\n '
        f'{preview_chain}'
    )

    if args.no_audio:
        audio_chain = ''
    else:
        if args.audio_source == 'alsa':
            audio_src = f'alsasrc device={args.alsa_device}'
        else:
            audio_src = 'audiotestsrc is-live=true wave=red-noise'
        audio_chain = (
            f'{audio_src} ! audioconvert ! audioresample ! '
            f'{audio_queue} ! opusenc ! rtpopuspay ! {rtp_queue} ! '
            'application/x-rtp,media=audio,encoding-name=OPUS,payload=96 ! sendrecv.'
        )

    pipeline = (
        'webrtcbin name=sendrecv bundle-policy=max-bundle '
        'stun-server=stun://stun.l.google.com:19302\n '
        f'{video_chain}\n '
        f'{audio_chain}\n'
    )
    return pipeline


def check_plugins(args):
    needed = [
        "webrtcbin",
        "opusenc",
        "rtpopuspay",
    ]
    if not args.no_audio:
        if args.audio_source == 'alsa':
            needed.append("alsasrc")
        else:
            needed.append("audiotestsrc")
    if args.video_source == 'test':
        needed.append("videotestsrc")
    elif args.video_source == 'libcamera':
        needed.append("libcamerasrc")
    elif args.video_source == 'v4l2':
        needed.append("v4l2src")

    if args.video_codec == 'vp8':
        needed.extend(["vp8enc", "rtpvp8pay"])
    elif args.video_codec == 'h264':
        needed.extend([args.h264_encoder, "h264parse", "rtph264pay"])

    missing = [name for name in needed if Gst.ElementFactory.find(name) is None]
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True


if __name__=='__main__':
    Gst.init(None)
    parser = argparse.ArgumentParser()
    parser.add_argument('peerid', nargs='?', default='browser',
                        help='String ID of the peer to connect to (default: browser)')
    parser.add_argument('--server', help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    parser.add_argument('--video-source', choices=['test', 'libcamera', 'v4l2'], default='test',
                        help='Video source to use (default: test)')
    parser.add_argument('--v4l2-device', default='/dev/video0',
                        help='V4L2 device path for USB webcam (default: /dev/video0)')
    parser.add_argument('--video-codec', choices=['vp8', 'h264'], default='h264',
                        help='Video codec to use (default: h264)')
    parser.add_argument('--h264-encoder', default='v4l2h264enc',
                        help='H.264 encoder element (default: v4l2h264enc)')
    parser.add_argument('--encoder-props', default='',
                        help='Additional encoder properties, e.g. "extra-controls=\"controls,video_bitrate=2000000\""')
    parser.add_argument('--width', type=int, default=0, help='Video width')
    parser.add_argument('--height', type=int, default=0, help='Video height')
    parser.add_argument('--framerate', type=int, default=0, help='Video framerate')
    parser.add_argument('--bitrate', type=int, default=0, help='Encoder bitrate (kbps, if supported)')
    parser.add_argument('--keyframe-interval', '--key-int', dest='keyframe_interval', type=int, default=30,
                        help='Keyframe interval for VP8 only (default: 30)')
    parser.add_argument('--rtp-mtu', type=int, default=0,
                        help='RTP MTU size (optional)')
    parser.add_argument('--audio-source', choices=['test', 'alsa'], default='test',
                        help='Audio source: test tone or ALSA mic (default: test)')
    parser.add_argument('--alsa-device', default='hw:3,0',
                        help='ALSA capture device for mic input (default: hw:3,0)')
    parser.add_argument('--no-audio', action='store_true', help='Disable audio')
    parser.add_argument('--preview', action='store_true', help='Show local preview on Pi')
    args = parser.parse_args()
    if not check_plugins(args):
        sys.exit(1)
    try:
        PIPELINE_DESC = build_pipeline(args)
    except ValueError as exc:
        print(exc)
        sys.exit(1)
    loop = asyncio.get_event_loop()
    while True:
        our_id = random.randrange(10, 10000)
        c = WebRTCClient(our_id, args.peerid, args.server)
        try:
            loop.run_until_complete(c.connect())
            res = loop.run_until_complete(c.loop())
        except Exception as e:
            print(f'Connection error: {e}')
            res = 0
        if res != 0:
            sys.exit(res)
        print('Peer disconnected — waiting 2s before reconnecting...')

        time.sleep(2)
