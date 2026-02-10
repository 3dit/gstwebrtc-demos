import random
import ssl
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

PIPELINE_DESC = '''
webrtcbin name=sendrecv bundle-policy=max-bundle stun-server=stun://stun.l.google.com:19302
 videotestsrc is-live=true pattern=ball ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay !
 queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
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
        text = self._ensure_h264_packetization_mode(text)
        print ('Sending offer:\n%s' % text)
        msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(msg))
        loop.close()

    def _ensure_h264_packetization_mode(self, sdp_text):
        lines = sdp_text.split('\n')
        has_h264 = any(line.startswith('a=rtpmap:97 H264/90000') for line in lines)
        if not has_h264:
            return sdp_text

        new_lines = []
        fmtp_updated = False
        for line in lines:
            if line.startswith('a=fmtp:97'):
                if line.endswith('\r'):
                    line = line[:-1]
                    line = line + ';packetization-mode=1' if 'packetization-mode=' not in line else line
                    line = line + ';profile-level-id=42e01f' if 'profile-level-id=' not in line else line
                    line = line + ';level-asymmetry-allowed=1' if 'level-asymmetry-allowed=' not in line else line
                    line = f'{line}\r'
                else:
                    line = line + ';packetization-mode=1' if 'packetization-mode=' not in line else line
                    line = line + ';profile-level-id=42e01f' if 'profile-level-id=' not in line else line
                    line = line + ';level-asymmetry-allowed=1' if 'level-asymmetry-allowed=' not in line else line
                fmtp_updated = True
            new_lines.append(line)

        if not fmtp_updated:
            for idx, line in enumerate(new_lines):
                if line.startswith('a=rtpmap:97 H264/90000'):
                    insert_line = 'a=fmtp:97 packetization-mode=1;profile-level-id=42e01f;level-asymmetry-allowed=1\r'
                    new_lines.insert(idx + 1, insert_line)
                    break

        return '\n'.join(new_lines)

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

    def on_connection_state_changed(self, element, _):
        state = element.get_property('connection-state')
        print('Peer connection state:', state.value_nick)

    def on_signaling_state_changed(self, element, _):
        state = element.get_property('signaling-state')
        print('Signaling state:', state.value_nick)

    def on_bus_error(self, _, msg):
        err, debug = msg.parse_error()
        print('GStreamer error:', err, debug)

    def on_bus_warning(self, _, msg):
        err, debug = msg.parse_warning()
        print('GStreamer warning:', err, debug)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
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
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self.on_bus_error)
        bus.connect('message::warning', self.on_bus_warning)
        self.pipe.set_state(Gst.State.PLAYING)
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
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL)
        self.pipe = None
        self.webrtc = None

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            if message == 'HELLO':
                await self.setup_call()
            elif message == 'SESSION_OK':
                self.start_pipeline()
            elif message.startswith('ERROR'):
                print (message)
                self.close_pipeline()
                return 1
            else:
                self.handle_sdp(message)
        self.close_pipeline()
        return 0

    async def stop(self):
        if self.conn:
            await self.conn.close()
        self.conn = None


def build_pipeline(args):
    video_queue = 'queue max-size-time=0 max-size-buffers=1 max-size-bytes=0 leaky=downstream'
    audio_queue = 'queue max-size-time=0 max-size-buffers=2 max-size-bytes=0 leaky=downstream'

    if args.video_source == 'test':
        video_src = 'videotestsrc is-live=true pattern=ball'
        video_caps = ''
    elif args.video_source == 'libcamera':
        video_src = 'libcamerasrc'
        if args.width and args.height and args.framerate:
            video_caps = f'video/x-raw,width={args.width},height={args.height},framerate={args.framerate}/1'
        else:
            video_caps = 'video/x-raw'
    else:
        raise ValueError(f"Unknown video source: {args.video_source}")

    if args.video_codec == 'vp8':
        video_enc = f'vp8enc deadline=1 keyframe-max-dist={args.keyframe_interval}'
        video_pay = 'rtpvp8pay pt=97 picture-id-mode=1'
        video_rtp_caps = (
            'capsfilter caps=application/x-rtp,media=video,encoding-name=VP8,payload=97,clock-rate=90000'
        )
    elif args.video_codec == 'h264':
        enc_props = ''
        if args.bitrate:
            if args.h264_encoder.startswith('v4l2h264enc') and not args.encoder_props:
                enc_props = f' extra-controls="controls,video_bitrate={args.bitrate * 1000}"'
            else:
                enc_props = f' bitrate={args.bitrate}'
        if args.encoder_props:
            enc_props = (enc_props + ' ' + args.encoder_props).strip()
        video_enc = f'{args.h264_encoder}{(" " + enc_props) if enc_props else ""}'
        video_pay = (
            'h264parse config-interval=1 ! '
            'capsfilter caps=video/x-h264,stream-format=avc,alignment=au ! '
            'rtph264pay pt=97 config-interval=1'
        )
        video_rtp_caps = (
            'capsfilter caps=application/x-rtp,media=video,encoding-name=H264,payload=97,clock-rate=90000,'
            'packetization-mode=1'
        )
    else:
        raise ValueError(f"Unknown video codec: {args.video_codec}")

    src_caps_part = f' ! capsfilter caps={video_caps}' if video_caps else ''
    format_caps_part = ''
    if args.video_codec == 'h264':
        format_caps_part = ' ! capsfilter caps=video/x-raw,format=NV12'
    elif args.video_codec == 'vp8':
        format_caps_part = ' ! capsfilter caps=video/x-raw,format=I420'
    preview_chain = ''
    if args.preview:
        preview_chain = f'vtee. ! queue ! autovideosink sync=false\n '

    video_chain = (
        f'{video_src}{src_caps_part} ! videoconvert{format_caps_part} ! tee name=vtee '\
        f'vtee. ! {video_queue} ! {video_enc} ! {video_pay} ! {video_queue} ! {video_rtp_caps} ! sendrecv.\n '
        f'{preview_chain}'
    )

    if args.no_audio:
        audio_chain = ''
    else:
        audio_chain = (
            'audiotestsrc is-live=true wave=red-noise ! audioconvert ! audioresample ! '
            f'{audio_queue} ! opusenc ! rtpopuspay ! {audio_queue} ! '
            'application/x-rtp,media=audio,encoding-name=OPUS,payload=96 ! sendrecv.'
        )

    pipeline = (
        'webrtcbin name=sendrecv bundle-policy=max-bundle '
        'stun-server=stun://stun.l.google.com:19302\n '
        f'{video_chain}\n '
        f'{audio_chain}\n'
    )
    return pipeline


def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True


if __name__=='__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('peerid', nargs='?', default='browser',
                        help='String ID of the peer to connect to (default: browser)')
    parser.add_argument('--server', help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    parser.add_argument('--video-source', choices=['test', 'libcamera'], default='test',
                        help='Video source to use (default: test)')
    parser.add_argument('--video-codec', choices=['vp8', 'h264'], default='vp8',
                        help='Video codec to use (default: vp8)')
    parser.add_argument('--h264-encoder', default='v4l2h264enc',
                        help='H.264 encoder element (default: v4l2h264enc)')
    parser.add_argument('--encoder-props', default='',
                        help='Additional encoder properties, e.g. "extra-controls=\"controls,video_bitrate=2000000\""')
    parser.add_argument('--width', type=int, default=0, help='Video width (libcamera only)')
    parser.add_argument('--height', type=int, default=0, help='Video height (libcamera only)')
    parser.add_argument('--framerate', type=int, default=0, help='Video framerate (libcamera only)')
    parser.add_argument('--bitrate', type=int, default=0, help='Encoder bitrate (kbps, if supported)')
    parser.add_argument('--keyframe-interval', type=int, default=30,
                        help='Keyframe interval for VP8 (default: 30)')
    parser.add_argument('--no-audio', action='store_true', help='Disable audio')
    parser.add_argument('--preview', action='store_true', help='Show local preview on Pi')
    args = parser.parse_args()
    try:
        PIPELINE_DESC = build_pipeline(args)
    except ValueError as exc:
        print(exc)
        sys.exit(1)
    our_id = random.randrange(10, 10000)
    c = WebRTCClient(our_id, args.peerid, args.server)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(c.connect())
    res = loop.run_until_complete(c.loop())
    sys.exit(res)
