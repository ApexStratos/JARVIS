import os
import sys
import streamlit as st
from streamlit_mic_recorder import speech_to_text

# --- 1. NEURAL PATHWAY CONFIGURATION ---
# We force the app directory into the search path so Python can find brain.py, etc.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

# --- 2. MODULE HANDSHAKE ---
try:
    import brain
    import engine
    import evolution
    
    # Map the classes to their identifiers
    JarvisBrain = brain.JarvisBrain
    TradeEngine = engine.TradeEngine
    EvolutionProtocol = evolution.EvolutionProtocol
except ImportError as e:
    st.error(f"⚠️ NEURAL LINK FAILURE: {e}")
    st.info("DIAGNOSTICS:")
    st.write(f"Direct Path: {CURRENT_DIR}")
    if os.path.exists(CURRENT_DIR):
        st.write(f"Detected Files: {os.listdir(CURRENT_DIR)}")
    st.stop()

# --- 3. UI INITIALIZATION ---
st.set_page_config(page_title="JARVIS: NEXUS-1", layout="wide", initial_sidebar_state="collapsed")

# Persistent System Instances
if 'jarvis' not in st.session_state:
    st.session_state.jarvis = JarvisBrain()
if 'engine' not in st.session_state:
    st.session_state.engine = TradeEngine()
if 'evolver' not in st.session_state:
    st.session_state.evolver = EvolutionProtocol()

# --- 4. THE NEURAL ORB (Interface) ---
st.markdown("""
    <style>
        .stApp { background-color: #000000; color: #00ffff; }
        #orb-container { display: flex; justify-content: center; align-items: center; height: 300px; }
    </style>
    <div id="orb-container"><canvas id="canvas"></canvas></div>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <script>
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(75, window.innerWidth/300, 0.1, 1000);
        const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('canvas'), alpha: true });
        renderer.setSize(window.innerWidth, 300);
        const geometry = new THREE.IcosahedronGeometry(1, 15);
        const material = new THREE.MeshBasicMaterial({ color: 0x00ffff, wireframe: true });
        const orb = new THREE.Mesh(geometry, material);
        scene.add(orb);
        camera.position.z = 2.5;
        function animate() {
            requestAnimationFrame(animate);
            orb.rotation.y += 0.005;
            orb.rotation.x += 0.002;
            renderer.render(scene, camera);
        }
        animate();
    </script>
    """, unsafe_allow_html=True)

# --- 5. SYSTEM LOGIC ---
def execute_system_tasks(jarvis_response):
    if "[SEARCH:" in jarvis_response:
        query = jarvis_response.split("[SEARCH:")[1].split("]")[0]
        st.write(f"🌐 *Accessing web resources for: {query}*")
    
    if "[UPDATE]" in jarvis_response:
        st.warning("Initiating Evolution Protocol...")
        success, msg = st.session_state.evolver.update_dna(
            "brain.py", 
            "# Evolution Pulse: Neural pathways optimized.", 
            "Self-Correction Fix"
        )
        st.write(msg)

# --- 6. COMMAND CENTER ---
st.title("J.A.R.V.I.S. | Nexus-1")

text_input = speech_to_text(
    language='en',
    start_prompt="Click to Speak, Sir",
    stop_prompt="Processing Directive...",
    just_once=True,
    key='STT'
)

if text_input:
    st.info(f"**Sir:** {text_input}")
    
    with st.spinner("Consulting protocols..."):
        try:
            # Check for financial keywords
            finance_report = ""
            if any(word in text_input.lower() for word in ["money", "balance", "trade", "account"]):
                finance_report = st.session_state.engine.get_report()
            
            # Brain Processing
            response_text = st.session_state.jarvis.think(text_input, financial_context=finance_report)
            
            st.success(f"**JARVIS:** {response_text}")
            execute_system_tasks(response_text)
            
        except Exception as e:
            st.error(f"Logic Error: {str(e)}")

# --- 7. SYSTEM VITALS ---
with st.expander("System Vitals & Diagnostics"):
    col1, col2 = st.columns(2)
    with col1:
        st.write("🛰️ **Status:** Operational")
        st.write(f"📁 **Active Path:** {CURRENT_DIR}")
    with col2:
        if st.button("Manual Trade Cycle"):
            st.session_state.engine.run_cycle()
            st.toast("Trade cycle complete.")
