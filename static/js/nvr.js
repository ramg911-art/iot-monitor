const GO2RTC_URL = "http://10.0.10.225:1984";

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
        const pc = new RTCPeerConnection();

        pc.ontrack = (event) => {
            videoEl.srcObject = event.streams[0];
        };

        const offer = await pc.createOffer({
            offerToReceiveVideo: true,
            offerToReceiveAudio: false
        });

        await pc.setLocalDescription(offer);

        const response = await fetch(
            `${GO2RTC_URL}/api/webrtc?src=${encodeURIComponent(streamName)}`,
            {
                method: "POST",
                headers: { "Content-Type": "application/sdp" },
                body: pc.localDescription.sdp
            }
        );

        if (!response.ok) {
            throw new Error("WebRTC request failed");
        }

        const answer = await response.text();

        await pc.setRemoteDescription({
            type: "answer",
            sdp: answer
        });

    } catch (err) {
        console.error("WebRTC error:", err);
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
