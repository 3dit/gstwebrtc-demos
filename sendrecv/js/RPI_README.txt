Streaming OV5647 camera to a web browser via WebRTC on Raspberry Pi 4 (Trixie)

Prerequisites:
    - Python venv at ~/gstwebrtc-demos/venv with websockets installed
    - GStreamer 1.26+ with gst-plugins-ugly (x264enc), libcamera
    - OV5647 camera (or other libcamera-supported camera)

Step 1: Start web server (serves the browser client)
Terminal A:
    cd ~/gstwebrtc-demos/sendrecv/js
    python -m http.server 8080

Step 2: Start signalling server (negotiates WebRTC connections)
Terminal B:
    cd ~/gstwebrtc-demos
    source venv/bin/activate
    python signalling/simple_server.py --addr 0.0.0.0 --port 8443 --disable-ssl

Step 3: Point browser to Pi
    http://<pi-ip-address>:8080/
    (e.g. http://192.168.1.136:8080/)
    The page will keep retrying the signalling server connection automatically.

Step 4: Start the sender (camera → WebRTC stream)
    The signalling server and browser should be connected before starting this.
Terminal C:
    cd ~/gstwebrtc-demos
    source venv/bin/activate
    python sendrecv/gst/webrtc_sendrecv.py \
        --server ws://127.0.0.1:8443 \
        --video-source libcamera \
        --video-codec h264 \
        --h264-encoder x264enc \
        --encoder-props "tune=zerolatency speed-preset=ultrafast key-int-max=24" \
        --width 480 --height 360 \
        --framerate 12 \
        --bitrate 800 \

    Add GST_DEBUG=2 before "python" for debug output.
    Add --preview to open the local preview window on the Pi desktop.

Notes:
    - Uses x264enc (software H264) with constrained-baseline profile for
      browser compatibility. v4l2h264enc hardware encoding is not currently
      working reliably on Pi 4.
    - A keyframe is automatically forced when DTLS completes so the browser
      can start decoding immediately.
    - The browser client retries the signalling server connection indefinitely,
      so you can restart the server/sender without refreshing the page.
    - The .local ICE candidate resolution errors in the sender terminal are
      harmless (mDNS privacy candidates from Chrome).
