const GO2RTC_URL = "http://10.0.10.225:1984";

/** Same-origin signaling URL (avoids CORS). Backend forwards to go2rtc; media still goes direct to go2rtc. */
function getWebrtcSignalUrl(streamName) {
    return `/api/cameras/webrtc-signal?src=${encodeURIComponent(streamName)}`;
}

async function fetchCameras() {
    const token = localStorage.getItem("iot_token") || "";
    const res = await fetch("/api/cameras/nvr-cameras?page=1&per_page=16", {
        headers: token ? { Authorization: "Bearer " + token } : {},
    });
    const data = await res.json();
    return data.items || data || [];
}

function renderCameras(cameras) {
    const container = document.getElementById("nvr-grid");
    if (!container) {
        console.error("nvr-grid container missing");
        return;
    }

    container.innerHTML = "";

    (cameras || []).forEach((cam) => {
        const card = document.createElement("div");
        card.className = "nvr-camera-card";
        card.dataset.stream = cam.stream_name;

        const video = document.createElement("video");
        video.autoplay = true;
        video.playsInline = true;
        video.muted = true;
        video.controls = false;
        video.style.width = "100%";
        video.style.height = "100%";
        video.style.objectFit = "cover";

        card.appendChild(video);
        container.appendChild(card);

        card.addEventListener("click", () => {
            startWebRTC(cam.stream_name, video);
        });
    });
}

async function startWebRTC(streamName, videoEl) {
    try {
        console.log("Starting WebRTC for:", streamName);

        const pc = new RTCPeerConnection({
            iceServers: []
        });

        pc.ontrack = (event) => {
            console.log("Track received");
            videoEl.srcObject = event.streams[0];
        };

        pc.oniceconnectionstatechange = () => {
            console.log("ICE state:", pc.iceConnectionState);
        };

        // Create offer
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        console.log("Sending SDP offer to go2rtc...");

        const response = await fetch(
            `http://10.0.10.225:1984/api/webrtc?src=${streamName}`,
            {
                method: "POST",
                headers: { "Content-Type": "application/sdp" },
                body: offer.sdp
            }
        );

        const answerSDP = await response.text();

        if (!response.ok) {
            console.error("go2rtc error response:", answerSDP);
            throw new Error(`WebRTC failed: ${response.status}`);
        }

        console.log("Received SDP answer");

        await pc.setRemoteDescription({
            type: "answer",
            sdp: answerSDP
        });

        console.log("WebRTC connected");

    } catch (err) {
        console.error("WebRTC fatal error:", err);
    }
}

document.addEventListener("DOMContentLoaded", async () => {
    const cameras = await fetchCameras();
    renderCameras(cameras);
});

window.GO2RTC_URL = GO2RTC_URL;
window.startWebRTC = startWebRTC;
window.fetchCameras = fetchCameras;
window.renderCameras = renderCameras;
window.refreshNvrCameras = async () => {
    const cameras = await fetchCameras();
    renderCameras(cameras);
};
