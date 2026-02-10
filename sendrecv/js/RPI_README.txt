To run this on RPI 4, trixie:

Start web server in sendrecv/js directory
Terminal A:
    cd ~/gstwebrtc-demos
    source venv/bin/activate
    cd sendrecv/js
    python -m http.server 8080

Start signal server - this is what negotiates WebRTC connection
Terminal B:
    cd ~/gstwebrtc-demos
    source venv/bin/activate
    python signalling/simple_server.py --addr 0.0.0.0 --port 8443 --disable-ssl

Point windows browser to Pi
    http://192.168.1.136:8080/
    
Start webrtc_sendrecv.py - this is the actual streaming code
Terminal C:
    cd ~/gstwebrtc-demos
    source venv/bin/activate
    python sendrecv/gst/webrtc_sendrecv.py --server ws://127.0.0.1:8443 --video-source libcamera --video-codec vp8 --width 480 --height 360 --framerate 12 --bitrate 600 --key-int 24 --rtp-mtu 1000
