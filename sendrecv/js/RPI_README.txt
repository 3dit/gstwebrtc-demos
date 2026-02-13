Streaming USB webcam video + microphone audio to a web browser via WebRTC
on Raspberry Pi 4 (Debian Trixie)

Prerequisites:
    - GStreamer 1.26+ with gst-plugins-ugly (x264enc)
    - Python 3 with a venv for the websockets package:
          python3 -m venv ~/gstwebrtc-demos/venv
          source ~/gstwebrtc-demos/venv/bin/activate
          pip install websockets
    - A USB webcam (e.g. Innomaker U20CAM, Logitech C270) for video + mic
    - (Optional) A CSI camera (e.g. OV5647) requires libcamera; no built-in mic

Identify your devices:
    Find your Pi's IP:  hostname -I   (first address is your LAN IP)
    Video devices:      v4l2-ctl --list-devices
    Audio devices:      arecord -l
    Quick camera test:  gst-launch-1.0 v4l2src device=/dev/video0 ! videoconvert ! autovideosink

    Example output:
        v4l2-ctl shows "Innomaker-U20CAM ... /dev/video0"  → use --v4l2-device /dev/video0
        arecord shows "card 2: ... device 0"                → use --alsa-device hw:2,0

Step 1: Start web server (serves the browser client)
Terminal A:
    cd ~/gstwebrtc-demos/sendrecv/js
    python -m http.server 8080

Step 2: Start signalling server
Terminal B:
    cd ~/gstwebrtc-demos && source venv/bin/activate
    python signalling/simple_server.py --addr 0.0.0.0 --port 8443 --disable-ssl

Step 3: Open the browser
    On any PC on the same network, open:  http://<pi-ip>:8080/
    The page shows "Connecting..." and retries automatically until the sender starts.

Step 4: Start the sender
Terminal C:
    cd ~/gstwebrtc-demos && source venv/bin/activate
    python sendrecv/gst/webrtc_sendrecv.py \
        --server ws://127.0.0.1:8443 \
        --video-source v4l2 \
        --v4l2-device /dev/video0 \
        --video-codec h264 \
        --h264-encoder x264enc \
        --encoder-props "tune=zerolatency speed-preset=ultrafast key-int-max=24" \
        --width 640 --height 480 \
        --framerate 30 \
        --bitrate 800 \
        --audio-source alsa \
        --alsa-device hw:2,0 \
        --preview

Exmaple for new camera
    cd ~/gstwebrtc-demos && source venv/bin/activate && DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 GST_DEBUG=2 python sendrecv/gst/webrtc_sendrecv.py --server ws://127.0.0.1:8443 --video-source v4l2 --v4l2-device /dev/video0 --video-codec h264 --h264-encoder x264enc --encoder-props "tune=zerolatency speed-preset=ultrafast key-int-max=24" --width 640 --height 480 --framerate 30 --bitrate 800 --audio-source alsa --alsa-device hw:2,0 --preview 2>&1
Example 15 fps
    cd ~/gstwebrtc-demos && source venv/bin/activate && DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 GST_DEBUG=2 python sendrecv/gst/webrtc_sendrecv.py --server ws://127.0.0.1:8443 --video-source v4l2 --v4l2-device /dev/video0 --video-codec h264 --h264-encoder x264enc --encoder-props "tune=zerolatency speed-preset=ultrafast key-int-max=24" --width 640 --height 480 --framerate 15 --bitrate 800 --audio-source alsa --alsa-device hw:2,0 --preview 2>&1

    Adjust --v4l2-device and --alsa-device to match your hardware (see above).

    For a CSI camera (libcamera) with no microphone:
        Replace: --video-source v4l2 --v4l2-device /dev/video0
        With:    --video-source libcamera
        Add:     --no-audio

    Other options:
        GST_DEBUG=2 python ...   Enable GStreamer debug logging
        --no-audio               Disable audio entirely
        --preview                Show local video preview on the Pi desktop

    If --preview doesn't open a window (e.g. running over SSH), prefix with:
        DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 python ...

What to expect:
    1. Video appears in the browser within a few seconds.
    2. Click the "Click to unmute audio" button to hear the microphone.
       (Chrome requires a user gesture before playing audio.)
    3. Reloading the browser page reconnects automatically (~4 seconds).
    4. The sender retries on any failure (ICE timeout, network glitch,
       browser close) — no manual restart needed.

Troubleshooting:
    - ".local" ICE resolution errors in the terminal are harmless. These are
      mDNS privacy candidates from Chrome that libnice cannot resolve. ICE
      succeeds via STUN server-reflexive candidates instead.
    - v4l2h264enc (hardware H264) is unreliable on Pi 4. Use x264enc (software)
      with constrained-baseline profile for maximum browser compatibility.
    - If video is black/frozen after connection, wait for the automatic keyframe
      (forced on DTLS completion) or reload the browser.
