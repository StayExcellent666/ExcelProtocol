import { useState, useEffect, useCallback, useRef } from "react";
import EmojiPickerLib, { Theme as EmojiTheme, EmojiStyle, SuggestionMode } from "emoji-picker-react";
import {
  DndContext, closestCenter, PointerSensor, KeyboardSensor,
  useSensor, useSensors,
} from "@dnd-kit/core";
import {
  SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy,
  useSortable, arrayMove,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

const API_BASE = import.meta.env.VITE_API_URL || "https://excelprotocol.fly.dev";

function useIsMobile() {
  const [isMobile, setIsMobile] = useState(() => window.innerWidth <= 768);
  useEffect(() => {
    const fn = () => setIsMobile(window.innerWidth <= 768);
    window.addEventListener("resize", fn);
    return () => window.removeEventListener("resize", fn);
  }, []);
  return isMobile;
}

// ── Global styles ─────────────────────────────────────────────────────────────
if (typeof document !== "undefined") {
  const s = document.createElement("style");
  s.textContent = `
    @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;800;900&family=Outfit:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
    *, *::before, *::after { box-sizing: border-box; }
    html, body { height: 100%; width: 100%; margin: 0; padding: 0; overflow: hidden; }
    #root { height: 100%; width: 100%; max-width: 100% !important; margin: 0 !important; padding: 0 !important; overflow: hidden; display: flex; flex-direction: column; }
    /* Override Vite default styles */
    body { min-width: unset !important; }
    #root > div { width: 100%; }
    :root {
      --bg:       #080b0f;
      --bg1:      #0d1117;
      --bg2:      #111820;
      --bg3:      #18212c;
      --border:   #1e2d3d;
      --border2:  #243444;
      --cyan:     #00f5d4;
      --cyan2:    #00c4aa;
      --cyan-dim: rgba(0,245,212,0.08);
      --cyan-glow:rgba(0,245,212,0.18);
      --text:     #e2eaf4;
      --text2:    #7a9ab5;
      --text3:    #3d5a73;
      --red:      #ff4d6d;
      --red-dim:  rgba(255,77,109,0.1);
      --green:    #39d98a;
      --yellow:   #f5c842;
      --glass-bg-card: rgba(15,21,30,0.95);
      --glass-border:  rgba(0,245,212,0.14);
      --glass-border2: rgba(0,245,212,0.28);
    }
      66%  { transform: translate(-56%,-46%) scale(0.95); opacity:0.45; }
      100% { transform: translate(-50%,-50%) scale(1);    opacity:0.4; }
    }
      80%  { transform: translate(-42%,-58%) scale(0.9);  opacity:0.2; }
      100% { transform: translate(-50%,-50%) scale(1);    opacity:0.25; }
    }
    select option { background: #0d1117; color: #e2eaf4; }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--cyan2); }
    textarea { resize: vertical; }
    * { transition: border-color 0.15s, background 0.15s, color 0.15s, box-shadow 0.15s; }
    button:hover { filter: brightness(1.1); }
    a:hover { opacity: 0.85; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes fadeIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
    @keyframes navExpand { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:translateY(0); } }
    /* ── Mobile ── */
    @media (max-width: 768px) {
      .mob-sidebar { display: none !important; }
      .mob-topbar-username { display: none !important; }
      .mob-topbar-logout { display: none !important; }
      .mob-nav-drawer { display: flex !important; }
      .mob-main-pad { padding: 14px 14px 24px 14px !important; }
      .mob-top-height { height: 48px !important; }
      .mob-stack { flex-direction: column !important; align-items: stretch !important; }
      .mob-full { width: 100% !important; box-sizing: border-box !important; }
    }
    @media (min-width: 769px) {
      .mob-nav-drawer { display: none !important; }
    }
    .mob-nav-drawer { display: none; position: fixed; inset: 0; z-index: 500; background: rgba(0,0,0,0.7); }
    @keyframes scanline {
      0% { transform: translateY(-100%); }
      100% { transform: translateY(100vh); }
    }
  `;
  document.head.appendChild(s);
}

// ── Auth ──────────────────────────────────────────────────────────────────────
// Session is now stored as an HttpOnly cookie set by the server.
// No token in localStorage or URL — the browser sends the cookie automatically.

async function apiFetch(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: "include",  // always send cookies
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) { window.location.href = "/auth/login"; throw new Error("Unauthorized"); }
  if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
  return res.json();
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function timeAgo(iso) {
  if (!iso) return "";
  // SQLite CURRENT_TIMESTAMP returns "YYYY-MM-DD HH:MM:SS" with no timezone info.
  // Append Z so JS parses it as UTC rather than local time.
  const normalized = iso.includes("T") ? iso : iso.replace(" ", "T") + "Z";
  const diff = Date.now() - new Date(normalized).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}
function roleColor(int) {
  if (!int) return "var(--text3)";
  return `#${int.toString(16).padStart(6, "0")}`;
}


// ── Design tokens ─────────────────────────────────────────────────────────────
const C = {
  input: {
    background:"rgba(8,11,15,0.95)", border:"1px solid rgba(0,245,212,0.13)", borderRadius:6,
    color:"var(--text)", padding:"9px 13px", fontSize:13, fontFamily:"'Orbitron',sans-serif",
    outline:"none", width:"100%", boxSizing:"border-box",
  },
  label: {
    fontSize:10, color:"var(--cyan2)", textTransform:"uppercase", letterSpacing:1.2,
    fontWeight:700, display:"block", marginBottom:5, fontFamily:"'JetBrains Mono',monospace",
  },
  btnPrimary: {
    padding:"8px 18px", borderRadius:6, border:"1px solid var(--cyan)", background:"var(--cyan-dim)",
    color:"var(--cyan)", cursor:"pointer", fontFamily:"'Outfit',sans-serif", fontSize:13, fontWeight:600,
    letterSpacing:0.2, boxShadow:"0 0 10px rgba(0,245,212,0.2)", textShadow:"0 0 8px rgba(0,245,212,0.5)",
  },
  btnSecondary: {
    padding:"8px 16px", borderRadius:6, border:"1px solid var(--border2)", background:"transparent",
    color:"var(--text2)", cursor:"pointer", fontFamily:"'Outfit',sans-serif", fontSize:13,
  },
  btnDanger: {
    padding:"6px 13px", borderRadius:6, border:"1px solid rgba(255,77,109,0.3)",
    background:"var(--red-dim)", color:"var(--red)", fontSize:12, cursor:"pointer",
    fontFamily:"'Outfit',sans-serif",
  },
  card: {
    background:"linear-gradient(135deg, rgba(20,30,42,0.97) 0%, rgba(13,19,28,0.99) 100%)",
    border:"1px solid rgba(0,245,212,0.13)",
    borderRadius:12, padding:"18px 20px",
    boxShadow:"0 2px 16px rgba(0,0,0,0.5), inset 0 1px 0 rgba(0,245,212,0.08), inset 0 -1px 0 rgba(0,0,0,0.3)",
  },
};

// ── Primitives ────────────────────────────────────────────────────────────────
function Avatar({ src, name, size=36, radius="50%" }) {
  const [err, setErr] = useState(false);
  const initials = name ? name.split(" ").map(w=>w[0]).slice(0,2).join("") : "?";
  if (src && !err) return <img src={src} onError={()=>setErr(true)} style={{ width:size, height:size, borderRadius:radius, flexShrink:0, objectFit:"cover" }} alt="" />;
  return <div style={{ width:size, height:size, borderRadius:radius, background:"linear-gradient(135deg,var(--cyan),#0088ff)", display:"flex", alignItems:"center", justifyContent:"center", fontSize:size*0.36, fontWeight:700, color:"#000", flexShrink:0 }}>{initials}</div>;
}
function GuildAvatar({ guild, size=34 }) {
  return <Avatar src={guild.icon?`https://cdn.discordapp.com/icons/${guild.id}/${guild.icon}.png`:null} name={guild.name} size={size} />;
}
function TwitchAvatar({ s, size=44 }) {
  return <Avatar src={s.profile_image_url} name={s.display_name||s.twitch_username} size={size} radius={8} />;
}
function UserAvatar({ user, size=28 }) {
  return <Avatar src={user?.avatar?`https://cdn.discordapp.com/avatars/${user.user_id}/${user.avatar}.png`:null} name={user?.username} size={size} />;
}

function Badge({ text, color }) {
  const c = color||"var(--cyan)";
  return <span style={{ padding:"2px 7px", borderRadius:3, fontSize:11, fontWeight:700, letterSpacing:0.8, textTransform:"uppercase", background:c+"22", color:c, border:`1px solid ${c}55`, fontFamily:"'JetBrains Mono',monospace", boxShadow:`0 0 8px ${c}33`, textShadow:`0 0 6px ${c}88` }}>{text}</span>;
}

function Spinner({ size=24 }) {
  return <div style={{ width:size, height:size, borderRadius:"50%", border:`2px solid var(--border2)`, borderTopColor:"var(--cyan)", animation:"spin 0.7s linear infinite", flexShrink:0 }} />;
}

// ── Particle Background ───────────────────────────────────────────────────────
function ParticleCanvas() {
  const canvasRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    let raf;
    const CYAN = "0,245,212";
    const BLUE = "64,168,255";
    const COUNT = 55;

    const resize = () => {
      canvas.width  = window.innerWidth;
      canvas.height = window.innerHeight;
    };
    resize();
    window.addEventListener("resize", resize);

    const particles = Array.from({ length: COUNT }, () => ({
      x:     Math.random() * window.innerWidth,
      y:     Math.random() * window.innerHeight,
      r:     Math.random() * 1.4 + 0.4,
      speed: Math.random() * 0.35 + 0.1,
      drift: (Math.random() - 0.5) * 0.18,
      alpha: Math.random() * 0.5 + 0.1,
      fade:  (Math.random() > 0.5 ? 1 : -1) * (Math.random() * 0.003 + 0.001),
      color: Math.random() > 0.65 ? BLUE : CYAN,
    }));

    const INTERVAL = 1000 / 30; // 30fps cap
    let last = 0;
    const draw = (ts) => {
      raf = requestAnimationFrame(draw);
      if (document.hidden) return; // pause when tab not visible
      if (ts - last < INTERVAL) return; // throttle to 30fps
      last = ts;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const p of particles) {
        p.y     -= p.speed;
        p.x     += p.drift;
        p.alpha += p.fade;
        if (p.alpha >= 0.65) { p.alpha = 0.65; p.fade = -Math.abs(p.fade); }
        if (p.alpha <= 0.05) { p.alpha = 0.05; p.fade =  Math.abs(p.fade); }
        if (p.y < -10) { p.y = canvas.height + 10; p.x = Math.random() * canvas.width; }
        if (p.x < -10 || p.x > canvas.width + 10) { p.x = Math.random() * canvas.width; }
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${p.color},${p.alpha.toFixed(3)})`;
        ctx.fill();
      }
    };
    draw(0);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", resize); };
  }, []);
  return <canvas ref={canvasRef} style={{ position:"fixed", inset:0, zIndex:0, pointerEvents:"none" }} />;
}

// ── Sidebar Particles ─────────────────────────────────────────────────────────
function SidebarParticles() {
  const canvasRef = useRef(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    let raf;
    const CYAN = "0,245,212";
    const BLUE = "64,168,255";
    const COUNT = 18;

    const resize = () => {
      const parent = canvas.parentElement;
      if (!parent) return;
      canvas.width  = parent.offsetWidth;
      canvas.height = parent.offsetHeight;
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas.parentElement);

    const particles = Array.from({ length: COUNT }, () => ({
      x:     Math.random() * 200,
      y:     Math.random() * window.innerHeight,
      r:     Math.random() * 1.2 + 0.3,
      speed: Math.random() * 0.3 + 0.08,
      drift: (Math.random() - 0.5) * 0.15,
      alpha: Math.random() * 0.45 + 0.08,
      fade:  (Math.random() > 0.5 ? 1 : -1) * (Math.random() * 0.003 + 0.001),
      color: Math.random() > 0.65 ? BLUE : CYAN,
    }));

    const INTERVAL = 1000 / 30;
    let last = 0;
    const draw = (ts) => {
      raf = requestAnimationFrame(draw);
      if (document.hidden) return;
      if (ts - last < INTERVAL) return;
      last = ts;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const p of particles) {
        p.y     -= p.speed;
        p.x     += p.drift;
        p.alpha += p.fade;
        if (p.alpha >= 0.5)  { p.alpha = 0.5;  p.fade = -Math.abs(p.fade); }
        if (p.alpha <= 0.04) { p.alpha = 0.04; p.fade =  Math.abs(p.fade); }
        if (p.y < -10) { p.y = canvas.height + 10; p.x = Math.random() * canvas.width; }
        if (p.x < -10 || p.x > canvas.width + 10) { p.x = Math.random() * canvas.width; }
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${p.color},${p.alpha.toFixed(3)})`;
        ctx.fill();
      }
    };
    draw(0);
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, []);
  return (
    <canvas
      ref={canvasRef}
      style={{
        position:"absolute", inset:0, zIndex:0, pointerEvents:"none",
        maskImage:"linear-gradient(to top, transparent 0%, transparent 15%, black 55%)",
        WebkitMaskImage:"linear-gradient(to top, transparent 0%, transparent 15%, black 55%)",
      }}
    />
  );
}

function NavIcon({ icon, size=16 }) {
  if (typeof icon === "string" && icon.endsWith(".png"))
    return <img src={icon} alt="" style={{ width:size, height:size, flexShrink:0, objectFit:"contain" }} />;
  return <span style={{ fontSize:size-1, flexShrink:0 }}>{icon}</span>;
}

function NavItem({ icon, label, active, onClick, count, large }) {
  const pad = large ? "11px 14px" : "8px 12px";
  const fs = large ? 15 : 13.5;
  return (
    <button onClick={onClick} style={{ display:"flex", alignItems:"center", gap:10, padding:pad, borderRadius:6, border:"none", background:active?"var(--cyan-dim)":"transparent", color:active?"var(--cyan)":"var(--text2)", cursor:"pointer", width:"100%", textAlign:"left", fontSize:fs, fontWeight:active?600:400, fontFamily:"'Outfit',sans-serif", borderLeft:active?"2px solid var(--cyan)":"2px solid transparent", position:"relative", zIndex:1, boxShadow:active?"inset 0 0 12px rgba(0,245,212,0.07)":"none", textShadow:active?"0 0 10px rgba(0,245,212,0.5)":"none" }}>
      <NavIcon icon={icon} size={large ? 22 : 20} />
      <span style={{ flex:1 }}>{label}</span>
      {count!=null && <span style={{ fontSize:10, background:"var(--bg3)", color:"var(--text3)", padding:"1px 6px", borderRadius:10, fontFamily:"'JetBrains Mono',monospace" }}>{count}</span>}
    </button>
  );
}

function NavGroup({ icon, label, activeTab, tabs, onSelect, large }) {
  const isActive = tabs.some(t => t.id === activeTab);
  const [open, setOpen] = useState(isActive);
  const pad = large ? "11px 14px" : "8px 12px";
  const fs = large ? 15 : 13.5;
  const subPad = large ? "10px 14px" : "7px 12px";
  const subFs = large ? 14 : 13;
  useEffect(() => { if (isActive) setOpen(true); }, [isActive]);
  return (
    <div style={{ position:"relative", zIndex:1 }}>
      <button onClick={() => setOpen(o => !o)} style={{ display:"flex", alignItems:"center", gap:10, padding:pad, borderRadius:6, border:"none", background:isActive?"var(--cyan-dim)":"transparent", color:isActive?"var(--cyan)":"var(--text2)", cursor:"pointer", width:"100%", textAlign:"left", fontSize:fs, fontWeight:isActive?600:400, fontFamily:"'Outfit',sans-serif", borderLeft:isActive?"2px solid var(--cyan)":"2px solid transparent" }}>
        <NavIcon icon={icon} size={large ? 22 : 20} />
        <span style={{ flex:1 }}>{label}</span>
        <span style={{ fontSize:11, color:"var(--cyan2)", display:"inline-block", transition:"transform 0.2s", transform:open?"rotate(180deg)":"rotate(0deg)", textShadow:"0 0 6px rgba(0,196,170,0.5)" }}>▼</span>
      </button>
      {open && (
        <div style={{ marginLeft:8, borderLeft:"1px solid var(--border)", paddingLeft:4, animation:"navExpand 0.15s ease" }}>
          {tabs.map(t => (
            <button key={t.id} onClick={() => onSelect(t.id)} style={{ display:"flex", alignItems:"center", gap:10, padding:subPad, borderRadius:6, border:"none", background:activeTab===t.id?"var(--cyan-dim)":"transparent", color:activeTab===t.id?"var(--cyan)":"var(--text2)", cursor:"pointer", width:"100%", textAlign:"left", fontSize:subFs, fontWeight:activeTab===t.id?600:400, fontFamily:"'Outfit',sans-serif", borderLeft:activeTab===t.id?"2px solid var(--cyan)":"2px solid transparent" }}>
              <NavIcon icon={t.icon} size={large ? 20 : 18} />
              <span>{t.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Modal({ onClose, children, width=460 }) {
  return (
    <div style={{ position:"fixed", inset:0, background:"rgba(0,0,0,0.85)", zIndex:200, display:"flex", alignItems:"center", justifyContent:"center", padding:20 }}>
      <div onClick={e=>e.stopPropagation()} style={{ background:"linear-gradient(160deg, rgba(18,26,38,0.99) 0%, rgba(11,17,26,0.99) 100%)", border:"1px solid rgba(0,245,212,0.22)", borderRadius:16, padding:28, width, maxWidth:"100%", maxHeight:"90vh", overflowY:"auto", display:"flex", flexDirection:"column", gap:18, boxShadow:"0 8px 48px rgba(0,0,0,0.8), 0 0 24px rgba(0,245,212,0.06), inset 0 1px 0 rgba(0,245,212,0.10)" }}>
        {children}
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return <div><label style={C.label}>{label}</label>{children}</div>;
}

function CyanInput(props) {
  return <input {...props} style={{ ...C.input, ...props.style }} onFocus={e=>e.target.style.borderColor="var(--cyan)"} onBlur={e=>e.target.style.borderColor="var(--border)"} />;
}

function CyanSelect({ value, onChange, children, style }) {
  return <select value={value} onChange={onChange} style={{ ...C.input, ...style }} onFocus={e=>e.target.style.borderColor="var(--cyan)"} onBlur={e=>e.target.style.borderColor="var(--border)"}>{children}</select>;
}

// ── Login Screen ──────────────────────────────────────────────────────────────
function LoginScreen() {
  return (
    <div style={{ height:"100vh", background:"var(--bg)", display:"flex", alignItems:"center", justifyContent:"center", flexDirection:"column", gap:32, position:"relative", overflow:"hidden" }}>
      {/* Scanline effect */}
      <div style={{ position:"absolute", inset:0, background:"repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,245,212,0.015) 2px, rgba(0,245,212,0.015) 4px)", pointerEvents:"none" }} />
      {/* Particle background */}
      <ParticleCanvas />

      {/* Glass card */}
      <div style={{ display:"flex", flexDirection:"column", alignItems:"center", gap:28, padding:"48px 52px", borderRadius:20, background:"linear-gradient(160deg, rgba(18,26,38,0.98) 0%, rgba(10,16,24,0.99) 100%)", border:"1px solid rgba(0,245,212,0.25)", boxShadow:"0 8px 48px rgba(0,0,0,0.7), 0 0 40px rgba(0,245,212,0.07), inset 0 1px 0 rgba(0,245,212,0.10)", animation:"fadeIn 0.4s ease" }}>

      <div style={{ display:"flex", alignItems:"center", gap:14 }}>
        <img src="/app/protocol.png" alt="ExcelProtocol" style={{ width:48, height:48, borderRadius:"50%", border:"1px solid var(--cyan)", boxShadow:"0 0 16px rgba(0,245,212,0.5), 0 0 40px rgba(0,245,212,0.2), 0 0 60px rgba(0,245,212,0.08)", objectFit:"cover", display:"block", flexShrink:0 }} />
        <div>
          <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:26, color:"var(--text)", letterSpacing:0, textShadow:"0 0 20px rgba(0,245,212,0.4), 0 0 40px rgba(0,245,212,0.15)" }}>ExcelProtocol</div>
          <div style={{ fontFamily:"'JetBrains Mono',monospace", fontSize:11, color:"var(--cyan2)", letterSpacing:1, textShadow:"0 0 10px rgba(0,196,170,0.7)" }}>DASHBOARD v2</div>
        </div>
      </div>

      <div style={{ textAlign:"center" }}>
        <p style={{ color:"var(--text2)", fontSize:14, margin:"0 0 6px", fontFamily:"'Outfit',sans-serif" }}>Manage your bot from the web</p>
        <p style={{ color:"var(--text3)", fontSize:12, margin:0, fontFamily:"'JetBrains Mono',monospace" }}>Only servers where you have Manage Server will appear</p>
      </div>

      <a href={`${API_BASE}/auth/login`} style={{ padding:"12px 32px", borderRadius:8, border:"1px solid var(--cyan)", background:"var(--cyan-dim)", color:"var(--cyan)", fontSize:14, fontWeight:600, cursor:"pointer", textDecoration:"none", display:"flex", alignItems:"center", gap:10, fontFamily:"'Outfit',sans-serif", boxShadow:"0 0 16px rgba(0,245,212,0.35), 0 0 32px rgba(0,245,212,0.12)", textShadow:"0 0 10px rgba(0,245,212,0.6)" }}>
        <span>🔐</span> Log in with Discord
      </a>

      </div>{/* /glass card */}
    </div>
  );
}

// ── Channel Select ────────────────────────────────────────────────────────────
function ChannelSelect({ guildId, value, onChange }) {
  const [channels, setChannels] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    apiFetch(`/api/guild/${guildId}/channels`).then(data => {
      setChannels(data.channels||[]);
      if (!value && data.default_channel_id) onChange(data.default_channel_id);
    }).catch(console.error).finally(()=>setLoading(false));
  }, [guildId]);
  if (loading) return <div style={{ color:"var(--text3)", fontSize:12, padding:"8px 0" }}>Loading channels...</div>;
  return (
    <CyanSelect value={String(value||"")} onChange={e=>onChange(e.target.value)}>
      <option value="">Select a channel...</option>
      {channels.map(c=><option key={c.id} value={c.id}>#{c.name}</option>)}
    </CyanSelect>
  );
}

// ── Emoji Picker ──────────────────────────────────────────────────────────────
function EmojiPicker({ guildId, value, onChange, onClose }) {
  const [guildEmojis, setGuildEmojis] = useState([]);
  const [tab, setTab] = useState("standard");
  const [serverSearch, setServerSearch] = useState("");
  const ref = useRef();
  const isMobile = useIsMobile();
  useEffect(() => {
    apiFetch(`/api/guild/${guildId}/emojis`).then(setGuildEmojis).catch(()=>{});
    const handler = e => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [guildId]);

  const filteredServer = guildEmojis.filter(
    e => !serverSearch || e.name.toLowerCase().includes(serverSearch.toLowerCase())
  );

  const mobileW = Math.min(340, window.innerWidth - 24);
  const pickerStyle = isMobile
    ? { position:"fixed", zIndex:600, background:"var(--bg1)", border:"1px solid var(--border2)", borderRadius:10, padding:tab==="standard"?0:12, width:mobileW, boxShadow:"0 8px 32px rgba(0,0,0,0.8)", top:"50%", left:"50%", transform:"translate(-50%,-50%)", maxHeight:"80vh", overflowY:"auto" }
    : { position:"absolute", zIndex:300, background:"var(--bg1)", border:"1px solid var(--border2)", borderRadius:10, padding:tab==="standard"?0:12, width:tab==="standard"?350:300, boxShadow:"0 8px 32px rgba(0,0,0,0.6)", top:"110%", left:0 };

  return (
    <div ref={ref} style={pickerStyle}>
      <div style={{ display:"flex", gap:4, padding:tab==="standard"?"12px 12px 8px":"0 0 10px", borderBottom: tab==="standard" ? "1px solid var(--border)" : "none" }}>
        {["standard","guild"].map(t=>(
          <button key={t} onClick={()=>setTab(t)} style={{ flex:1, padding:"6px 8px", borderRadius:5, border:tab===t?"1px solid var(--cyan)":"1px solid var(--border)", background:tab===t?"var(--cyan-dim)":"transparent", color:tab===t?"var(--cyan)":"var(--text2)", cursor:"pointer", fontSize:11, fontFamily:"'Outfit',sans-serif", fontWeight:600 }}>
            {t==="standard"?"Standard":`Server (${guildEmojis.length})`}
          </button>
        ))}
      </div>

      {tab === "standard" ? (
        // emoji-picker-react provides its own search bar, category tabs,
        // skin-tone selector, and ~3700 emojis. We theme it to roughly
        // match the dashboard's dark+cyan look via the `theme` prop.
        <EmojiPickerLib
          onEmojiClick={(emojiData) => {
            onChange(emojiData.emoji);
            onClose();
          }}
          theme={EmojiTheme.DARK}
          emojiStyle={EmojiStyle.NATIVE}
          suggestedEmojisMode={SuggestionMode.FREQUENT}
          searchPlaceHolder="Search emojis…"
          width={isMobile ? mobileW : 350}
          height={isMobile ? 360 : 400}
          previewConfig={{ showPreview: false }}
          lazyLoadEmojis={true}
        />
      ) : (
        <>
          <CyanInput value={serverSearch} onChange={e=>setServerSearch(e.target.value)} placeholder="Search server emojis..." style={{ marginBottom:10, fontSize:12 }} />
          <div style={{ display:"flex", flexWrap:"wrap", gap:3, maxHeight:240, overflowY:"auto" }}>
            {filteredServer.map(e=>(
              <button key={e.id} onClick={()=>{onChange(`<:${e.name}:${e.id}>`);onClose();}} style={{ width:34, height:34, borderRadius:5, border:value===`<:${e.name}:${e.id}>`?"1px solid var(--cyan)":"1px solid transparent", background:value===`<:${e.name}:${e.id}>`?"var(--cyan-dim)":"transparent", cursor:"pointer", padding:2 }}>
                <img src={`https://cdn.discordapp.com/emojis/${e.id}.${e.animated?"gif":"png"}`} style={{ width:26, height:26 }} alt={e.name} />
              </button>
            ))}
            {filteredServer.length===0 && <div style={{ color:"var(--text3)", fontSize:12, padding:"10px 0", width:"100%", textAlign:"center" }}>No server emojis found</div>}
          </div>
        </>
      )}
    </div>
  );
}

// ── Role Picker ───────────────────────────────────────────────────────────────
function RolePicker({ guildId, value, onChange }) {
  const [roles, setRoles] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => { apiFetch(`/api/guild/${guildId}/roles`).then(setRoles).catch(console.error).finally(()=>setLoading(false)); }, [guildId]);
  if (loading) return <div style={{ color:"var(--text3)", fontSize:12 }}>Loading roles...</div>;
  return (
    <CyanSelect value={value} onChange={e=>onChange(e.target.value)}>
      <option value="">Select a role...</option>
      <option value="__create__">➕ Create new role</option>
      {roles.filter(r=>r.name!=="@everyone").map(r=>(
        <option key={r.id} value={r.id}>@{r.name}</option>
      ))}
    </CyanSelect>
  );
}

// ── Streamer Modals ───────────────────────────────────────────────────────────
function AddStreamerModal({ guildId, onClose, onAdded }) {
  const [username, setUsername] = useState("");
  const [channelId, setChannelId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const submit = async () => {
    if (!username.trim()||!channelId) { setError("Both fields required."); return; }
    setLoading(true); setError("");
    try { await apiFetch(`/api/guild/${guildId}/streamers`, { method:"POST", body:JSON.stringify({ twitch_username:username.trim().toLowerCase(), channel_id:channelId }) }); onAdded(); onClose(); }
    catch(e) { setError(e.message.includes("409")?"Already tracked.":"Failed to add."); }
    setLoading(false);
  };
  return (
    <Modal onClose={onClose} width={420}>
      <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:17, color:"var(--text)", textShadow:"0 0 16px rgba(0,245,212,0.35), 0 0 32px rgba(0,245,212,0.12)" }}>Add Streamer</div>
      <Field label="Twitch Username"><CyanInput value={username} onChange={e=>setUsername(e.target.value)} placeholder="e.g. stayexcellent666" /></Field>
      <Field label="Notification Channel"><ChannelSelect guildId={guildId} value={channelId} onChange={setChannelId} /></Field>
      {error && <div style={{ color:"var(--red)", fontSize:12 }}>{error}</div>}
      <div style={{ display:"flex", gap:8, justifyContent:"flex-end" }}>
        <button onClick={onClose} style={C.btnSecondary}>Cancel</button>
        <button onClick={submit} disabled={loading} style={C.btnPrimary}>{loading?"Adding...":"Add Streamer"}</button>
      </div>
    </Modal>
  );
}

function memberAvatar(m) {
  if (!m) return "https://cdn.discordapp.com/embed/avatars/0.png";
  if (m.avatar) return `https://cdn.discordapp.com/avatars/${m.id}/${m.avatar}.png?size=32`;
  return "https://cdn.discordapp.com/embed/avatars/0.png";
}

function MemberPicker({ guildId, value, onChange }) {
  const [members, setMembers] = useState([]);
  const [search, setSearch]   = useState("");
  const [loading, setLoading] = useState(false);
  const selected = value ? members.find(m => String(m.id) === String(value)) : null;
  useEffect(() => {
    if (!guildId) return;
    setLoading(true);
    apiFetch(`/api/guild/${guildId}/members`)
      .then(r => setMembers(Array.isArray(r) ? r : (r?.members || [])))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [guildId]);
  const q = search.toLowerCase();
  const filtered = q
    ? members.filter(m => ((m.display_name||"").toLowerCase()).includes(q) || ((m.username||"").toLowerCase()).includes(q)).slice(0,100)
    : members.slice(0,100);
  return (
    <div style={{ border:"1px solid var(--border)", borderRadius:8, overflow:"hidden" }}>
      <div style={{ padding:"6px 10px", borderBottom:"1px solid var(--border)", background:"var(--bg2)" }}>
        {selected && (
          <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:6 }}>
            <div style={{ width:22, height:22, borderRadius:"50%", overflow:"hidden", flexShrink:0 }}>
              <img src={memberAvatar(selected)} style={{ width:"100%", height:"100%" }} alt=""
                onError={e=>{ if(!e.target.dataset.err){e.target.dataset.err=1;e.target.src="https://cdn.discordapp.com/embed/avatars/0.png";}}} />
            </div>
            <span style={{ fontSize:13, color:"var(--cyan)", fontWeight:500 }}>{selected.display_name}</span>
            <button onClick={() => onChange(null)} style={{ marginLeft:"auto", ...C.btnDanger, padding:"2px 8px", fontSize:11 }}>✕</button>
          </div>
        )}
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder={loading ? "Loading members…" : "Search members…"} disabled={loading}
          style={{ ...C.input, padding:"5px 10px", fontSize:12, width:"100%", boxSizing:"border-box" }}
          onFocus={e=>e.target.style.borderColor="var(--cyan)"} onBlur={e=>e.target.style.borderColor="var(--border)"} />
      </div>
      <div style={{ maxHeight:200, overflowY:"auto" }}>
        <div onClick={() => onChange(null)}
          style={{ padding:"6px 12px", cursor:"pointer", color:"var(--text3)", fontSize:12, borderBottom:"1px solid var(--border)" }}
          onMouseEnter={e=>e.currentTarget.style.background="var(--bg2)"}
          onMouseLeave={e=>e.currentTarget.style.background="transparent"}>— No link</div>
        {filtered.map(m => (
          <div key={m.id} onClick={() => onChange(m.id)}
            style={{ padding:"6px 12px", cursor:"pointer", display:"flex", alignItems:"center", gap:8, background:String(m.id)===String(value)?"rgba(0,245,212,0.08)":"transparent" }}
            onMouseEnter={e=>{ if(String(m.id)!==String(value)) e.currentTarget.style.background="var(--bg2)"; }}
            onMouseLeave={e=>{ e.currentTarget.style.background=String(m.id)===String(value)?"rgba(0,245,212,0.08)":"transparent"; }}>
            <div style={{ width:24, height:24, borderRadius:"50%", overflow:"hidden", flexShrink:0 }}>
              <img src={memberAvatar(m)} style={{ width:"100%", height:"100%" }} alt=""
                onError={e=>{ if(!e.target.dataset.err){e.target.dataset.err=1;e.target.src="https://cdn.discordapp.com/embed/avatars/0.png";}}} />
            </div>
            <div style={{ flex:1, minWidth:0 }}>
              <div style={{ fontSize:12, color:"var(--text)", fontWeight:500, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{m.display_name}</div>
              <div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>@{m.username}</div>
            </div>
            {String(m.id)===String(value) && <span style={{ color:"var(--cyan)", fontSize:12 }}>✓</span>}
          </div>
        ))}
        {!loading && filtered.length===0 && <div style={{ padding:"12px", color:"var(--text3)", fontSize:12, textAlign:"center" }}>No members found</div>}
      </div>
    </div>
  );
}

function EditStreamerModal({ guildId, streamer, onClose, onSaved }) {
  const originalChannelId = String(streamer.custom_channel_id||streamer.channel_id||"");
  const [channelId, setChannelId]         = useState(originalChannelId);
  const [discordUserId, setDiscordUserId] = useState(streamer.discord_user_id || null);
  const [loading, setLoading]             = useState(false);
  const save = async () => {
    setLoading(true);
    try {
      if (channelId && channelId !== originalChannelId) {
        await apiFetch(`/api/guild/${guildId}/streamers/${encodeURIComponent(streamer.twitch_username)}`, { method:"PATCH", body:JSON.stringify({ channel_id:channelId }) });
      }
      await apiFetch(`/api/guild/${guildId}/streamers/${encodeURIComponent(streamer.twitch_username)}/discord`, { method:"PATCH", body:JSON.stringify({ discord_user_id: discordUserId || null }) });
      onSaved(); onClose();
    } catch(e) { console.error(e); }
    setLoading(false);
  };
  return (
    <Modal onClose={onClose} width={480}>
      <div style={{ display:"flex", alignItems:"center", gap:12 }}>
        <TwitchAvatar s={streamer} size={38} />
        <div>
          <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:16, color:"var(--text)" }}>{streamer.display_name||streamer.twitch_username}</div>
          <div style={{ fontSize:11, color:"var(--cyan2)", fontFamily:"'JetBrains Mono',monospace" }}>twitch.tv/{streamer.twitch_username}</div>
        </div>
      </div>
      <Field label="Notification Channel"><ChannelSelect guildId={guildId} value={channelId} onChange={setChannelId} /></Field>
      <Field label="Discord Member (for Live Role)">
        <MemberPicker guildId={guildId} value={discordUserId} onChange={setDiscordUserId} />
        <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>Link to assign the live role when they go live</div>
      </Field>
      <div style={{ display:"flex", gap:8, justifyContent:"flex-end" }}>
        <button onClick={onClose} style={C.btnSecondary}>Cancel</button>
        <button onClick={save} disabled={loading} style={C.btnPrimary}>{loading?"Saving...":"Save"}</button>
      </div>
    </Modal>
  );
}

// ── Reaction Role Editor ──────────────────────────────────────────────────────
function RoleRowEditor({ id, guildId, role, onChange, onRemove }) {
  const [showEmoji, setShowEmoji] = useState(false);
  // Drag-reorder: each row is a sortable item. The grip handle has its own
  // listeners so dragging only starts from the handle, not the whole row
  // (otherwise tapping inputs would accidentally start drags).
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id });
  const dragStyle = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : 1,
    zIndex: isDragging ? 10 : "auto",
  };
  return (
    <div ref={setNodeRef} style={{ ...dragStyle, background:"var(--bg)", border:"1px solid var(--border)", borderRadius:8, padding:"12px 14px", display:"flex", flexDirection:"column", gap:10 }} {...attributes}>
      <div style={{ display:"flex", gap:8, alignItems:"flex-end" }}>
        {/* Drag handle — cursor:grab, only this element starts the drag */}
        <button
          {...listeners}
          aria-label="Drag to reorder"
          style={{ width:28, height:40, background:"transparent", border:"none", color:"var(--text3)", cursor:"grab", fontSize:18, lineHeight:1, padding:0, marginBottom:1, touchAction:"none", display:"flex", alignItems:"center", justifyContent:"center" }}
          onMouseDown={(e)=>e.preventDefault() /* prevent text selection */}
        >
          ⋮⋮
        </button>
        <div style={{ position:"relative", flexShrink:0 }}>
          <label style={C.label}>Emoji</label>
          <button onClick={()=>setShowEmoji(v=>!v)} style={{ width:40, height:40, borderRadius:7, border:"1px solid var(--border2)", background:"var(--bg2)", cursor:"pointer", fontSize:18, display:"flex", alignItems:"center", justifyContent:"center" }}>
            {role.emoji ? (role.emoji.startsWith("<:") ? <img src={`https://cdn.discordapp.com/emojis/${role.emoji.match(/\d+/)?.[0]}.png`} style={{ width:22, height:22 }} alt="" /> : role.emoji) : <span style={{ color:"var(--text3)", fontSize:16 }}>+</span>}
          </button>
          {showEmoji && <EmojiPicker guildId={guildId} value={role.emoji} onChange={v=>onChange({...role, emoji:v})} onClose={()=>setShowEmoji(false)} />}
        </div>
        <div style={{ flex:1 }}><label style={C.label}>Label</label><CyanInput value={role.label||""} onChange={e=>onChange({...role, label:e.target.value})} placeholder="e.g. Overwatch" /></div>
        <button onClick={onRemove} style={{ ...C.btnDanger, marginBottom:1, flexShrink:0 }}>✕</button>
      </div>
      <div><label style={C.label}>Discord Role</label><RolePicker guildId={guildId} value={role.role_id||""} onChange={v=>onChange({...role, role_id:v})} /></div>
      {role.role_id==="__create__" && <div><label style={C.label}>New Role Name</label><CyanInput value={role.new_role_name||""} onChange={e=>onChange({...role, new_role_name:e.target.value})} placeholder="New role name..." /></div>}
    </div>
  );
}

function ReactionRolePanelModal({ guildId, panel, onClose, onSaved }) {
  const isEdit = !!panel;
  const [title, setTitle] = useState(panel?.title||"");
  const [bodyText, setBodyText] = useState(panel?.body_text||"");
  const [type, setType] = useState(panel?.type||"dropdown");
  const [onlyAdd, setOnlyAdd] = useState(!!panel?.only_add);
  const [maxRoles, setMaxRoles] = useState(panel?.max_roles||"");
  const [channelId, setChannelId] = useState(panel ? String(panel.channel_id) : "");
  // Each role needs a stable id for drag-reorder + React key. Generate one
  // per row that survives reordering. Use a monotonic counter so we don't
  // depend on crypto APIs (some embedded clients lack `crypto.randomUUID`).
  const nextIdRef = useRef(0);
  const newId = () => `r${Date.now()}_${nextIdRef.current++}`;
  const [roles, setRoles] = useState(() =>
    (panel?.roles || []).map(r => ({ ...r, _uiId: newId() }))
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const addRole = () => setRoles(r => [...r, { label:"", emoji:"", role_id:"", _uiId: newId() }]);
  const updateRole = (uiId, val) => setRoles(r => r.map(x => x._uiId === uiId ? { ...val, _uiId: uiId } : x));
  const removeRole = (uiId) => setRoles(r => r.filter(x => x._uiId !== uiId));

  // Drag-reorder sensors: PointerSensor + small activation distance so a
  // pointerdown that immediately moves to click doesn't accidentally start
  // a drag (lets the user click the drag handle without instantly dragging).
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const handleDragEnd = (event) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    setRoles(items => {
      const oldIndex = items.findIndex(i => i._uiId === active.id);
      const newIndex = items.findIndex(i => i._uiId === over.id);
      if (oldIndex < 0 || newIndex < 0) return items;
      return arrayMove(items, oldIndex, newIndex);
    });
  };
  const save = async () => {
    if (!title.trim()) { setError("Title is required."); return; }
    if (!channelId && !isEdit) { setError("Channel is required."); return; }
    if (!roles.length) { setError("Add at least one role."); return; }
    for (const r of roles) {
      if (!r.label.trim()) { setError("All roles need a label."); return; }
      if (!r.role_id) { setError("All roles need a Discord role."); return; }
    }
    setLoading(true); setError("");
    // Backend doesn't need our UI-only drag ID; strip before sending.
    // Order in the array IS the new role order (already reordered locally
    // by drag handler), so no separate "order" field is needed.
    const payloadRoles = roles.map(({ _uiId, ...rest }) => rest);
    try {
      if (isEdit) {
        await apiFetch(`/api/guild/${guildId}/reaction-roles/${panel.message_id}`, { method:"PATCH", body:JSON.stringify({ title, body_text:bodyText.trim()||null, type, only_add:onlyAdd, max_roles:maxRoles?parseInt(maxRoles):null, roles:payloadRoles }) });
      } else {
        await apiFetch(`/api/guild/${guildId}/reaction-roles`, { method:"POST", body:JSON.stringify({ title, body_text:bodyText.trim()||null, type, only_add:onlyAdd, max_roles:maxRoles?parseInt(maxRoles):null, channel_id:channelId, roles:payloadRoles }) });
      }
      onSaved(); onClose();
    } catch(e) { setError(e.message||"Failed to save."); }
    setLoading(false);
  };
  return (
    <Modal onClose={onClose} width={520}>
      <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:17, color:"var(--text)", textShadow:"0 0 16px rgba(0,245,212,0.35), 0 0 32px rgba(0,245,212,0.12)" }}>{isEdit?"Edit Panel":"Create Reaction Role Panel"}</div>
      <Field label="Panel Title"><CyanInput value={title} onChange={e=>setTitle(e.target.value)} placeholder="e.g. Choose your roles" /></Field>
      <Field label="Body Text (optional)">
        <textarea
          value={bodyText}
          onChange={e=>setBodyText(e.target.value)}
          placeholder="e.g. Pick the roles that apply to you. You can select multiple."
          maxLength={2000}
          rows={3}
          style={{ ...C.input, resize:"vertical", fontFamily:"'Outfit',sans-serif", fontSize:13, lineHeight:1.5 }}
          onFocus={e=>e.target.style.borderColor="var(--cyan)"}
          onBlur={e=>e.target.style.borderColor="var(--border)"}
        />
        <div style={{ fontSize:11, color:"var(--text3)", marginTop:3, fontFamily:"'JetBrains Mono',monospace" }}>{bodyText.length}/2000</div>
      </Field>
      {!isEdit && <Field label="Channel"><ChannelSelect guildId={guildId} value={channelId} onChange={setChannelId} /></Field>}
      <div style={{ display:"flex", gap:10 }}>
        <div style={{ flex:1 }}><Field label="Type"><CyanSelect value={type} onChange={e=>setType(e.target.value)}><option value="dropdown">Dropdown</option><option value="buttons">Buttons</option></CyanSelect></Field></div>
        <div style={{ flex:1 }}><Field label="Max Roles"><CyanInput value={maxRoles} onChange={e=>setMaxRoles(e.target.value)} type="number" min="1" placeholder="No limit" /></Field></div>
      </div>
      <label style={{ display:"flex", alignItems:"center", gap:8, cursor:"pointer", fontSize:13, color:"var(--text2)", fontFamily:"'Orbitron',sans-serif" }}>
        <input type="checkbox" checked={onlyAdd} onChange={e=>setOnlyAdd(e.target.checked)} style={{ width:14, height:14, accentColor:"var(--cyan)" }} />
        Only allow adding roles (not removing)
      </label>
      <div>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:10 }}>
          <label style={C.label}>Role Buttons ({roles.length})</label>
          <button onClick={addRole} style={{ ...C.btnPrimary, padding:"5px 12px", fontSize:11 }}>+ Add Role</button>
        </div>
        <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={roles.map(r => r._uiId)} strategy={verticalListSortingStrategy}>
              {roles.map(r => (
                <RoleRowEditor
                  key={r._uiId}
                  id={r._uiId}
                  guildId={guildId}
                  role={r}
                  onChange={v => updateRole(r._uiId, v)}
                  onRemove={() => removeRole(r._uiId)}
                />
              ))}
            </SortableContext>
          </DndContext>
          {!roles.length && <div style={{ textAlign:"center", padding:"18px 0", color:"var(--text3)", fontSize:13, border:"1px dashed var(--border)", borderRadius:8, fontFamily:"'Outfit',sans-serif" }}>No roles yet — click + Add Role</div>}
        </div>
      </div>
      {error && <div style={{ color:"var(--red)", fontSize:12 }}>{error}</div>}
      <div style={{ display:"flex", gap:8, justifyContent:"flex-end" }}>
        <button onClick={onClose} style={C.btnSecondary}>Cancel</button>
        <button onClick={save} disabled={loading} style={C.btnPrimary}>{loading?"Saving...":isEdit?"Save Changes":"Create Panel"}</button>
      </div>
    </Modal>
  );
}

// ── Page header ───────────────────────────────────────────────────────────────
function PageHeader({ title, subtitle, action }) {
  return (
    <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:24, flexShrink:0 }}>
      <div>
        <h2 style={{ margin:0, fontSize:20, fontWeight:800, fontFamily:"'Orbitron',sans-serif", color:"var(--text)", letterSpacing:0, textShadow:"0 0 20px rgba(0,245,212,0.35), 0 0 40px rgba(0,245,212,0.12)" }}>{title}</h2>
        {subtitle && <p style={{ margin:"3px 0 0", color:"var(--text3)", fontSize:13, fontFamily:"'JetBrains Mono',monospace" }}>{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

// ── Streamers Tab ─────────────────────────────────────────────────────────────
function StreamersTab({ guildId, isDev, showAdd: showAddProp, onAddClose }) {
  const [streamers, setStreamers] = useState([]);
  const [limit, setLimit]         = useState(75);
  const [count, setCount]         = useState(0);
  const [loading, setLoading]     = useState(true);
  const [showAdd, setShowAdd]     = useState(false);
  const isAddOpen = showAdd || showAddProp;
  const closeAdd = () => { setShowAdd(false); onAddClose && onAddClose(); };
  const [editS, setEditS]         = useState(null);
  const [editLimit, setEditLimit] = useState(false);
  const [newLimit, setNewLimit]   = useState(75);
  const [savingLimit, setSavingLimit] = useState(false);
  const [search, setSearch]       = useState("");
  const [unresolvable, setUnresolvable] = useState(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [data, unres] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/streamers`),
        apiFetch(`/api/guild/${guildId}/unresolvable-streamers`).catch(() => []),
      ]);
      if (Array.isArray(data)) {
        setStreamers(data); setCount(data.length); setLimit(75);
      } else {
        setStreamers(data.streamers||[]); setCount(data.count||0); setLimit(data.limit||75);
        setNewLimit(data.limit||75);
      }
      setUnresolvable(new Set((unres||[]).map(r => r.streamer_name.toLowerCase())));
    } catch(e) { console.error(e); }
    setLoading(false);
  }, [guildId]);

  useEffect(()=>{ load(); },[load]);

  const remove = async username => {
    if (!confirm(`Remove ${username}?`)) return;
    await apiFetch(`/api/guild/${guildId}/streamers/${encodeURIComponent(username)}`, { method:"DELETE" });
    load();
  };

  const saveLimit = async () => {
    setSavingLimit(true);
    try {
      await apiFetch(`/api/guild/${guildId}/streamer-limit`, { method:"PATCH", body:JSON.stringify({ limit: parseInt(newLimit) }) });
      setLimit(parseInt(newLimit));
      setEditLimit(false);
    } catch(e) { alert("Failed: " + e.message); }
    setSavingLimit(false);
  };

  const atLimit = count >= limit;
  const limitColor = count >= limit ? "var(--red)" : count >= limit * 0.8 ? "var(--yellow)" : "var(--text3)";
  const filtered = search.trim()
    ? streamers.filter(s => (s.display_name||s.twitch_username).toLowerCase().includes(search.toLowerCase()))
    : streamers;

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%" }}>
      {(showAdd || showAddProp) && <AddStreamerModal guildId={guildId} onClose={()=>{ setShowAdd(false); onAddClose&&onAddClose(); }} onAdded={load} />}
      {editS && <EditStreamerModal guildId={guildId} streamer={editS} onClose={()=>setEditS(null)} onSaved={load} />}
      {/* Static header — never scrolls */}
      <div style={{ flexShrink:0, paddingBottom:12, marginBottom:-4 }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:8 }}>
          <div>
            <h2 style={{ margin:0, fontSize:20, fontWeight:800, fontFamily:"'Orbitron',sans-serif", color:"var(--text)", letterSpacing:0, textShadow:"0 0 20px rgba(0,245,212,0.35), 0 0 40px rgba(0,245,212,0.12)" }}>Stream Notifications</h2>
            <p style={{ margin:"3px 0 0", color:"var(--text3)", fontSize:13, fontFamily:"'JetBrains Mono',monospace" }}>
              <span style={{ color:limitColor }}>{count}/{limit}</span>
              <span> tracked streamers</span>
              {isDev && !editLimit && <button onClick={()=>setEditLimit(true)} style={{ marginLeft:10, fontSize:10, padding:"1px 7px", borderRadius:4, border:"1px solid var(--border2)", background:"transparent", color:"var(--text3)", cursor:"pointer", fontFamily:"'JetBrains Mono',monospace" }}>edit limit</button>}
              {isDev && editLimit && (
                <span style={{ marginLeft:10, display:"inline-flex", alignItems:"center", gap:6 }}>
                  <input type="number" min={1} value={newLimit} onChange={e=>setNewLimit(e.target.value)}
                    style={{ width:60, padding:"1px 6px", borderRadius:4, border:"1px solid var(--cyan)", background:"var(--bg1)", color:"var(--text)", fontSize:12, fontFamily:"'JetBrains Mono',monospace" }} />
                  <button onClick={saveLimit} disabled={savingLimit} style={{ fontSize:10, padding:"2px 8px", borderRadius:4, border:"1px solid var(--cyan)", background:"var(--cyan-dim)", color:"var(--cyan)", cursor:"pointer" }}>{savingLimit?"...":"save"}</button>
                  <button onClick={()=>setEditLimit(false)} style={{ fontSize:10, padding:"2px 8px", borderRadius:4, border:"1px solid var(--border2)", background:"transparent", color:"var(--text3)", cursor:"pointer" }}>cancel</button>
                </span>
              )}
            </p>
          </div>
          <div className="mob-stack" style={{ display:"flex", alignItems:"center", gap:8 }}>
            <input
              value={search}
              onChange={e=>setSearch(e.target.value)}
              placeholder="Search streamers..."
              className="mob-full"
              style={{ padding:"6px 12px", borderRadius:7, border:"1px solid var(--border)", background:"var(--bg2)", color:"var(--text)", fontSize:12, fontFamily:"'Outfit',sans-serif", width:180, outline:"none" }}
              onFocus={e=>e.target.style.borderColor="var(--cyan)"}
              onBlur={e=>e.target.style.borderColor="var(--border)"}
            />
            <button onClick={()=>setShowAdd(true)} disabled={atLimit && !isDev} className="mob-full" style={{ ...C.btnPrimary, opacity:atLimit && !isDev?0.4:1, cursor:atLimit && !isDev?"not-allowed":"pointer" }}>+ Add Streamer</button>
          </div>
        </div>
        {atLimit && !isDev && <div style={{ marginBottom:8, padding:"8px 14px", borderRadius:8, border:"1px solid rgba(255,77,109,0.3)", background:"var(--red-dim)", color:"var(--red)", fontSize:12, fontFamily:"'Outfit',sans-serif" }}>⚠️ Streamer limit reached ({count}/{limit}). Contact the bot owner to increase your limit.</div>}
      </div>
      {/* Scrollable list only */}
      <div style={{ flex:1, overflowY:"auto", minHeight:0, paddingRight:4 }}>
        {loading ? <div style={{ display:"flex", justifyContent:"center", padding:40 }}><Spinner /></div> : (
          <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
            {filtered.map((s,i)=>{
              const isUnresolvable = unresolvable.has((s.twitch_username||"").toLowerCase());
              return (
              <div key={i} style={{ ...C.card, display:"flex", alignItems:"center", gap:14, padding:"14px 18px", border: isUnresolvable ? "1px solid rgba(255,193,7,0.4)" : C.card.border, flexWrap:"wrap" }}>
                <TwitchAvatar s={s} size={44} />
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:3, flexWrap:"wrap" }}>
                    <span style={{ fontWeight:700, fontSize:15, fontFamily:"'Orbitron',sans-serif", color:"var(--text)" }}>{s.display_name||s.twitch_username}</span>
                    <a href={`https://twitch.tv/${s.twitch_username}`} target="_blank" rel="noreferrer" style={{ fontSize:12, color:"var(--cyan2)", textDecoration:"none", fontFamily:"'JetBrains Mono',monospace", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", maxWidth:160 }}>↗ twitch.tv/{s.twitch_username}</a>
                    {isUnresolvable && (
                      <span style={{ position:"relative", display:"inline-flex", alignItems:"center" }}
                        onMouseEnter={e=>e.currentTarget.querySelector(".rr-tip").style.opacity="1"}
                        onMouseLeave={e=>e.currentTarget.querySelector(".rr-tip").style.opacity="0"}>
                        <span style={{ fontSize:11, padding:"2px 7px", borderRadius:4, background:"rgba(255,193,7,0.15)", border:"1px solid rgba(255,193,7,0.4)", color:"var(--yellow)", fontFamily:"'JetBrains Mono',monospace", cursor:"help" }}>
                          ⚠ unresolvable
                        </span>
                        <span className="rr-tip" style={{
                          opacity:0, transition:"opacity 0.15s", pointerEvents:"none",
                          position:"absolute", top:"calc(100% + 6px)", left:0, zIndex:100,
                          background:"#1a2332", border:"1px solid rgba(255,193,7,0.3)",
                          borderRadius:7, padding:"8px 12px", width:280,
                          fontSize:12, color:"#e2d88a", fontFamily:"'Outfit',sans-serif",
                          lineHeight:1.5, boxShadow:"0 4px 16px rgba(0,0,0,0.4)"
                        }}>
                          This Twitch account couldn't be found — likely banned, deleted or renamed. EventSub notifications won't fire. Remove it and re-add with the correct username to fix.
                        </span>
                      </span>
                    )}
                  </div>
                  {s.description && <div style={{ fontSize:12, color:"var(--text3)", marginBottom:3, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", maxWidth:"70%" }}>{s.description}</div>}
                  <div style={{ display:"flex", alignItems:"center", gap:10, flexWrap:"wrap" }}>
                    <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>📢 {s.effective_channel_name||s.channel_name||s.channel_id}</div>
                    {s.discord_user_id && <div style={{ fontSize:11, color:"var(--cyan2)", fontFamily:"'JetBrains Mono',monospace", background:"rgba(0,245,212,0.06)", border:"1px solid rgba(0,245,212,0.15)", borderRadius:4, padding:"1px 6px" }}>🎮 {s.discord_display_name||s.discord_user_id}</div>}
                  </div>
                </div>
                <div style={{ display:"flex", gap:6, flexShrink:0 }}>
                  <button onClick={()=>setEditS(s)} style={{ ...C.btnSecondary, padding:"5px 12px", fontSize:12 }}>Edit</button>
                  <button onClick={()=>remove(s.twitch_username)} style={C.btnDanger}>Remove</button>
                </div>
              </div>
              );
            })}
            {!filtered.length && <div style={{ textAlign:"center", padding:"60px 0", color:"var(--text3)" }}><div style={{ fontSize:32, marginBottom:12 }}>{search ? "🔍" : "📺"}</div><div style={{ fontFamily:"'Outfit',sans-serif", fontSize:13 }}>{search ? `No streamers matching "${search}"` : "No streamers tracked yet"}</div></div>}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Reaction Roles Tab ────────────────────────────────────────────────────────
function ReactionRolesTab({ guildId }) {
  const [panels, setPanels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editPanel, setEditPanel] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const load = useCallback(async () => { setLoading(true); apiFetch(`/api/guild/${guildId}/reaction-roles`).then(setPanels).catch(console.error).finally(()=>setLoading(false)); }, [guildId]);
  useEffect(()=>{ load(); },[load]);
  const deletePanel = async messageId => {
    if (!confirm("Delete this panel from Discord?")) return;
    await apiFetch(`/api/guild/${guildId}/reaction-roles/${messageId}`, { method:"DELETE" });
    setPanels(p=>p.filter(r=>r.message_id!==messageId));
  };
  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%" }}>
      {showCreate && <ReactionRolePanelModal guildId={guildId} panel={null} onClose={()=>setShowCreate(false)} onSaved={load} />}
      {editPanel && <ReactionRolePanelModal guildId={guildId} panel={editPanel} onClose={()=>setEditPanel(null)} onSaved={load} />}
      <PageHeader title="Reaction Roles" subtitle={`${panels.length} panels`} action={<button onClick={()=>setShowCreate(true)} style={C.btnPrimary}>+ Create Panel</button>} />
      {loading ? <div style={{ display:"flex", justifyContent:"center", padding:40 }}><Spinner /></div> : (
        <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
          {panels.map((panel,i)=>(
            <div key={i} style={C.card}>
              <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:12 }}>
                <div>
                  <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--text)", marginBottom:6 }}>{panel.title}</div>
                  <div style={{ display:"flex", gap:8, flexWrap:"wrap", alignItems:"center" }}>
                    <span style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>📢 {panel.channel_name||panel.channel_id}</span>
                    <Badge text={panel.type} color="var(--cyan)" />
                    {panel.max_roles && <Badge text={`max ${panel.max_roles}`} color="var(--yellow)" />}
                    {panel.only_add && <Badge text="add-only" color="var(--green)" />}
                  </div>
                </div>
                <div style={{ display:"flex", gap:6 }}>
                  <button onClick={()=>setEditPanel(panel)} style={{ ...C.btnSecondary, padding:"5px 12px", fontSize:12 }}>Edit</button>
                  <button onClick={()=>deletePanel(panel.message_id)} style={{ ...C.btnDanger, padding:"5px 12px" }}>Delete</button>
                </div>
              </div>
              {panel.roles?.length>0 && (
                <div style={{ display:"flex", flexWrap:"wrap", gap:6 }}>
                  {panel.roles.map((r,j)=>{
                    const rc = roleColor(r.role_color);
                    return (
                      <div key={j} style={{ background:`${rc}12`, border:`1px solid ${rc}33`, borderRadius:6, padding:"5px 10px", fontSize:12, display:"flex", alignItems:"center", gap:5 }}>
                        {r.emoji && (r.emoji.startsWith("<:") ? <img src={`https://cdn.discordapp.com/emojis/${r.emoji.match(/\d+/)?.[0]}.png`} style={{ width:16, height:16 }} alt="" /> : <span style={{ fontSize:14 }}>{r.emoji}</span>)}
                        <span style={{ color:"var(--text)", fontWeight:600, fontFamily:"'Outfit',sans-serif", fontSize:13 }}>{r.label}</span>
                        <span style={{ color:rc, fontSize:10, fontFamily:"'JetBrains Mono',monospace" }}>@{r.role_name}</span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
          {!panels.length && <div style={{ textAlign:"center", padding:"60px 0", color:"var(--text3)" }}><div style={{ fontSize:32, marginBottom:12 }}>🎭</div><div style={{ fontFamily:"'Outfit',sans-serif", fontSize:13 }}>No panels yet — click + Create Panel</div></div>}
        </div>
      )}
    </div>
  );
}

// ── Server Stats Tab ──────────────────────────────────────────────────────────
function ServerStatsTab({ guildId }) {
  const [stats, setStats]         = useState([]);
  const [channels, setChannels]   = useState([]);
  const [loading, setLoading]     = useState(true);
  const [saving, setSaving]       = useState(false);
  const [form, setForm]           = useState({ channel_id: "", format: "Members: {count}" });
  const [error, setError]         = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [st, ch] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/stat-channels`),
        apiFetch(`/api/guild/${guildId}/channels`),
      ]);
      setStats(st || []);
      const voiceChannels = (ch.voice_channels || []);
      setChannels(voiceChannels);
      if (voiceChannels.length && !form.channel_id) {
        setForm(p => ({ ...p, channel_id: voiceChannels[0].id }));
      }
    } catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    if (!form.channel_id) { setError("Select a voice channel."); return; }
    if (!form.format.includes("{count}")) { setError("Format must include {count}."); return; }
    setSaving(true); setError("");
    try {
      await apiFetch(`/api/guild/${guildId}/stat-channels`, {
        method: "POST",
        body: JSON.stringify({ channel_id: form.channel_id, format: form.format }),
      });
      setForm({ channel_id: "", format: "Members: {count}" });
      load();
    } catch(e) { setError(e.message || "Failed to save."); }
    finally { setSaving(false); }
  };

  const remove = async (channelId) => {
    if (!confirm("Remove this stat channel?")) return;
    try {
      await apiFetch(`/api/guild/${guildId}/stat-channels/${channelId}`, { method: "DELETE" });
      load();
    } catch(e) { alert("Failed to remove: " + e.message); }
  };

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  return (
    <div>
      <PageHeader title="Server Stats" subtitle="Display live member counts in voice channel names" />

      {/* ── Add Stat Channel ── */}
      <div style={{ ...C.card, marginBottom:16 }}>
        <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:17, color:"var(--text)", textShadow:"0 0 16px rgba(0,245,212,0.35), 0 0 32px rgba(0,245,212,0.12)", marginBottom:18 }}>
          Add Stat Channel
        </div>
        <div style={{ display:"flex", flexDirection:"column", gap:14 }}>
          <Field label="Voice Channel">
            {channels.length === 0 ? (
              <div style={{ fontSize:13, color:"var(--text3)", fontFamily:"'Outfit',sans-serif" }}>No voice channels found in this server.</div>
            ) : (
              <CyanSelect value={form.channel_id} onChange={e => setForm(p => ({ ...p, channel_id: e.target.value }))}>
                <option value="">Select voice channel...</option>
                {channels.map(c => <option key={c.id} value={c.id}>🔊 {c.name}</option>)}
              </CyanSelect>
            )}
          </Field>
          <Field label="Format">
            <CyanInput
              value={form.format}
              onChange={e => setForm(p => ({ ...p, format: e.target.value }))}
              placeholder="Members: {count}"
            />
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>
              Use <span style={{ color:"var(--cyan)" }}>{"{count}"}</span> as the placeholder — e.g. <span style={{ color:"var(--text2)" }}>👥 {"{count}"} members</span>
            </div>
          </Field>
          {error && <div style={{ color:"var(--red)", fontSize:12, fontFamily:"'Outfit',sans-serif" }}>{error}</div>}
          <div style={{ display:"flex", justifyContent:"flex-end" }}>
            <button onClick={save} disabled={saving} style={C.btnPrimary}>{saving ? "Saving…" : "Add Channel"}</button>
          </div>
        </div>
      </div>

      {/* ── Configured Stat Channels ── */}
      <div style={{ ...C.card }}>
        <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:17, color:"var(--text)", textShadow:"0 0 16px rgba(0,245,212,0.35), 0 0 32px rgba(0,245,212,0.12)", marginBottom:18 }}>
          Configured Channels
        </div>
        {stats.length === 0 ? (
          <div style={{ textAlign:"center", padding:"32px 0", color:"var(--text3)" }}>
            <div style={{ fontSize:28, marginBottom:8 }}>📊</div>
            <div style={{ fontSize:13, fontFamily:"'Outfit',sans-serif" }}>No stat channels configured yet</div>
          </div>
        ) : (
          <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
            {stats.map(s => (
              <div key={s.channel_id} style={{ display:"flex", alignItems:"center", gap:12, padding:"12px 14px", borderRadius:8, background:"rgba(8,11,15,0.6)", border:"1px solid var(--border)" }}>
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:13, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>🔊 {s.channel_name}</div>
                  <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>{s.format}</div>
                  {s.last_updated && (
                    <div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>
                      Last updated: {new Date(s.last_updated + "Z").toLocaleString()}
                    </div>
                  )}
                </div>
                <button onClick={() => remove(s.channel_id)} style={C.btnDanger}>Remove</button>
              </div>
            ))}
          </div>
        )}
        <div style={{ fontSize:11, color:"var(--text3)", marginTop:16, fontFamily:"'JetBrains Mono',monospace" }}>
          Updates every 15 minutes · Discord limits channel name edits to 2 per 10 minutes
        </div>
      </div>
    </div>
  );
}

function SafetyTab({ guildId }) {
  const [settings, setSettings] = useState(null);
  const [kicks, setKicks]       = useState([]);
  const [roles, setRoles]       = useState([]);
  const [loading, setLoading]   = useState(true);
  const [saving, setSaving]     = useState(false);
  const [form, setForm] = useState({
    enabled: false, min_account_age_days: 7, check_username_pattern: true,
    check_no_avatar: true, action: "kick", bypass_role_id: "", dm_on_kick: true,
  });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, k, rl] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/safety-settings`),
        apiFetch(`/api/guild/${guildId}/safety-kicks`),
        apiFetch(`/api/guild/${guildId}/roles`),
      ]);
      setSettings(s);
      setKicks(k);
      setRoles(rl || []);
      setForm({
        enabled: s.enabled || false,
        min_account_age_days: s.min_account_age_days ?? 7,
        check_username_pattern: s.check_username_pattern ?? true,
        check_no_avatar: s.check_no_avatar ?? true,
        action: s.action || "kick",
        bypass_role_id: s.bypass_role_id || "",
        dm_on_kick: s.dm_on_kick ?? true,
      });
    } catch(e) { console.error(e); }
    setLoading(false);
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      await apiFetch(`/api/guild/${guildId}/safety-settings`, {
        method: "POST",
        body: JSON.stringify({
          ...form,
          bypass_role_id: form.bypass_role_id || null,
        }),
      });
      await load();
    } catch(e) { alert("Failed: " + e.message); }
    setSaving(false);
  };

  const Toggle = ({ value, onChange }) => (
    <button onClick={() => onChange(!value)}
      style={{ width:44, height:24, borderRadius:12, border:"none", cursor:"pointer",
        background: value ? "var(--cyan)" : "var(--border2)", position:"relative",
        transition:"background 0.2s", flexShrink:0,
        boxShadow: value ? "0 0 10px rgba(0,245,212,0.4)" : "none" }}>
      <div style={{ position:"absolute", top:3, left: value ? 22 : 3, width:18, height:18,
        borderRadius:"50%", background:"#fff", transition:"left 0.2s" }} />
    </button>
  );

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  return (
    <div>
      <PageHeader title="Safety" subtitle="Protect your server from bot accounts and DM spammers" />

      {/* ── New Account Filter ── */}
      <div style={{ ...C.card, marginBottom:16 }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:18 }}>
          {sectionHead("New Account Filter", "Auto-kick or ban suspicious new members")}
          <Toggle value={form.enabled} onChange={v => setForm(p => ({ ...p, enabled: v }))} />
        </div>

        <div style={{ display:"flex", flexDirection:"column", gap:14, opacity: form.enabled ? 1 : 0.5, pointerEvents: form.enabled ? "auto" : "none" }}>

          <Field label="Minimum Account Age (days)">
            <CyanInput type="number" min={1} max={365} value={form.min_account_age_days}
              onChange={e => setForm(p => ({ ...p, min_account_age_days: parseInt(e.target.value) || 7 }))} />
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>
              Accounts newer than this will be flagged
            </div>
          </Field>

          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"10px 0", borderTop:"1px solid var(--border)" }}>
            <div>
              <div style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif", fontWeight:500 }}>Flag suspicious username patterns</div>
              <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>e.g. playful_hare_06276</div>
            </div>
            <Toggle value={form.check_username_pattern} onChange={v => setForm(p => ({ ...p, check_username_pattern: v }))} />
          </div>

          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"10px 0", borderTop:"1px solid var(--border)" }}>
            <div>
              <div style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif", fontWeight:500 }}>Flag accounts with no profile picture</div>
              <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>Default Discord avatar</div>
            </div>
            <Toggle value={form.check_no_avatar} onChange={v => setForm(p => ({ ...p, check_no_avatar: v }))} />
          </div>

          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"10px 0", borderTop:"1px solid var(--border)" }}>
            <div>
              <div style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif", fontWeight:500 }}>DM user on kick/ban</div>
              <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>Tell them why and how to rejoin</div>
            </div>
            <Toggle value={form.dm_on_kick} onChange={v => setForm(p => ({ ...p, dm_on_kick: v }))} />
          </div>

          <Field label="Action">
            <CyanSelect value={form.action} onChange={e => setForm(p => ({ ...p, action: e.target.value }))}>
              <option value="kick">Kick (can rejoin later)</option>
              <option value="ban">Ban (permanent)</option>
            </CyanSelect>
          </Field>

          <Field label="Bypass Role (optional)">
            <CyanSelect value={form.bypass_role_id} onChange={e => setForm(p => ({ ...p, bypass_role_id: e.target.value }))}>
              <option value="">None</option>
              {roles.map(r => <option key={r.id} value={r.id}>@{r.name}</option>)}
            </CyanSelect>
          </Field>
        </div>

        <div style={{ marginTop:18, display:"flex", justifyContent:"flex-end" }}>
          <button onClick={save} disabled={saving} style={{ ...C.btnPrimary, opacity: saving ? 0.6 : 1 }}>
            {saving ? "Saving..." : "Save Settings"}
          </button>
        </div>
      </div>

      {/* ── Kick Log ── */}
      <div style={{ ...C.card }}>
        {sectionHead("Action Log", `Last 7 days — ${kicks.length} action${kicks.length !== 1 ? "s" : ""}`)}
        {kicks.length === 0 ? (
          <div style={{ textAlign:"center", padding:"32px 0", color:"var(--text3)" }}>
            <div style={{ fontSize:28, marginBottom:8 }}>✅</div>
            <div style={{ fontSize:13, fontFamily:"'Outfit',sans-serif" }}>No kicks or bans recorded yet</div>
          </div>
        ) : (
          <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
            {kicks.map((k, i) => (
              <div key={i} style={{ display:"flex", alignItems:"center", gap:12, padding:"10px 14px", borderRadius:8, background:"rgba(8,11,15,0.6)", border:"1px solid var(--border)" }}>
                <div style={{ fontSize:18 }}>{k.action === "ban" ? "🔨" : "👢"}</div>
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{ fontSize:13, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>{k.username}</div>
                  <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{k.reason}</div>
                </div>
                <div style={{ textAlign:"right", flexShrink:0 }}>
                  <div style={{ fontSize:11, color: k.action === "ban" ? "var(--red)" : "var(--yellow)", fontFamily:"'JetBrains Mono',monospace", fontWeight:600 }}>{k.action.toUpperCase()}</div>
                  <div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>{timeAgo(k.kicked_at)}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}


// ── Notif Log Tab ─────────────────────────────────────────────────────────────
function NotifLogTab({ guildId }) {
  const [log, setLog] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(()=>{ apiFetch(`/api/guild/${guildId}/notiflog`).then(setLog).catch(console.error).finally(()=>setLoading(false)); },[guildId]);
  return (
    <div>
      <PageHeader title="Notification Log" subtitle="Last 30 days" />
      {loading ? <div style={{ display:"flex", justifyContent:"center", padding:40 }}><Spinner /></div> : (
        <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
          {log.map((entry,i)=>(
            <div key={i} style={{ ...C.card, padding:"11px 16px", display:"flex", alignItems:"center", gap:12 }}>
              <Avatar src={entry.profile_image_url} name={entry.display_name||entry.twitch_username} size={34} radius={6} />
              <div style={{ flex:1 }}>
                <a href={`https://twitch.tv/${entry.twitch_username}`} target="_blank" rel="noreferrer" style={{ color:"var(--cyan)", textDecoration:"none", fontWeight:700, fontSize:14, fontFamily:"'Orbitron',sans-serif" }}>{entry.display_name||entry.twitch_username}</a>
                <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>📢 {entry.channel_name||entry.channel_id}</div>
              </div>
              <Badge text={entry.event||"sent"} color={["sent","Sent"].includes(entry.event)?"var(--green)":"var(--red)"} />
              <span style={{ fontSize:11, color:"var(--text3)", minWidth:56, textAlign:"right", fontFamily:"'JetBrains Mono',monospace" }}>{timeAgo(entry.timestamp)}</span>
            </div>
          ))}
          {!log.length && <div style={{ textAlign:"center", padding:"60px 0", color:"var(--text3)" }}><div style={{ fontSize:32, marginBottom:12 }}>📋</div><div style={{ fontFamily:"'Outfit',sans-serif", fontSize:13 }}>No events in last 30 days</div></div>}
        </div>
      )}
    </div>
  );
}

// ── Suggestions Tab ───────────────────────────────────────────────────────────
function SuggestionsTab({ guildId }) {
  const [suggestText, setSuggestText] = useState("");
  const [suggestStatus, setSuggestStatus] = useState(null);
  const [supportText, setSupportText] = useState("");
  const [supportStatus, setSupportStatus] = useState(null);

  const submitSuggestion = async () => {
    if (!suggestText.trim()) return;
    setSuggestStatus("sending");
    try {
      await apiFetch("/api/suggest", { method:"POST", body:JSON.stringify({ text:suggestText.trim() }) });
      setSuggestStatus("sent"); setSuggestText(""); setTimeout(()=>setSuggestStatus(null),4000);
    } catch(e) { setSuggestStatus("error"); setTimeout(()=>setSuggestStatus(null),4000); }
  };

  const submitSupport = async () => {
    if (!supportText.trim()) return;
    setSupportStatus("sending");
    try {
      await apiFetch("/api/support", { method:"POST", body:JSON.stringify({ text:supportText.trim(), guild_id:guildId }) });
      setSupportStatus("sent"); setSupportText(""); setTimeout(()=>setSupportStatus(null),4000);
    } catch(e) { setSupportStatus("error"); setTimeout(()=>setSupportStatus(null),4000); }
  };

  const formFooter = (len, status, onSend, disabled) => (
    <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginTop:10 }}>
      <span style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{len}/1000</span>
      <div style={{ display:"flex", alignItems:"center", gap:12 }}>
        {status==="sent"  && <span style={{ fontSize:12, color:"var(--green)", fontFamily:"'JetBrains Mono',monospace" }}>✓ sent</span>}
        {status==="error" && <span style={{ fontSize:12, color:"var(--red)", fontFamily:"'JetBrains Mono',monospace" }}>✗ failed</span>}
        <button onClick={onSend} disabled={disabled||status==="sending"} style={{ ...C.btnPrimary, opacity:disabled||status==="sending"?0.4:1 }}>{status==="sending"?"Sending...":"Send"}</button>
      </div>
    </div>
  );

  return (
    <div>
      <PageHeader title="Contact" subtitle="Suggestions & support for ExcelProtocol" />

      {/* Suggestion card */}
      <div style={{ ...C.card, marginBottom:16, borderColor:"var(--border2)" }}>
        <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", marginBottom:4 }}>💡 Submit a Suggestion</div>
        <div style={{ fontSize:13, color:"var(--text3)", marginBottom:12, fontFamily:"'Outfit',sans-serif" }}>Drop a good idea and your server's streamer limit might just get a little bump 👀</div>
        <textarea value={suggestText} onChange={e=>setSuggestText(e.target.value)} placeholder="Describe your idea..." maxLength={1000} rows={4}
          style={{ ...C.input, lineHeight:1.5 }} onFocus={e=>e.target.style.borderColor="var(--cyan)"} onBlur={e=>e.target.style.borderColor="var(--border)"} />
        {formFooter(suggestText.length, suggestStatus, submitSuggestion, !suggestText.trim())}
      </div>

      {/* Support card */}
      <div style={{ ...C.card, borderColor:"var(--border2)" }}>
        <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", marginBottom:4 }}>🎫 Need Help?</div>
        <div style={{ fontSize:13, color:"var(--text3)", marginBottom:12, fontFamily:"'Outfit',sans-serif" }}>Having an issue with the bot? Send a message and I'll get back to you.</div>
        <textarea value={supportText} onChange={e=>setSupportText(e.target.value)} placeholder="Describe your issue..." maxLength={1000} rows={4}
          style={{ ...C.input, lineHeight:1.5 }} onFocus={e=>e.target.style.borderColor="var(--cyan)"} onBlur={e=>e.target.style.borderColor="var(--border)"} />
        {formFooter(supportText.length, supportStatus, submitSupport, !supportText.trim())}
      </div>
    </div>
  );
}


// ── Shared Helpers ────────────────────────────────────────────────────────────
const sectionHead = (title, sub) => (
  <div style={{ marginBottom:18 }}>
    <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:17, color:"var(--text)", textShadow:"0 0 12px rgba(0,245,212,0.2)" }}>{title}</div>
    {sub && <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", marginTop:3 }}>{sub}</div>}
  </div>
);

// ── Welcome & Goodbye Banner Settings (sub-section of Server Settings) ────────
function WelcomeSettings({ guildId, channels }) {
  const [cfg, setCfg] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [savedFlash, setSavedFlash] = useState(false);
  const [previewUrl, setPreviewUrl] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewWhich, setPreviewWhich] = useState("welcome");

  // Text channels only — welcome/goodbye banner must be a text channel
  const textChannels = (channels || []).filter(c => c.type === 0 || c.type === undefined);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch(`/api/guild/${guildId}/welcome-settings`);
      setCfg(data);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const update = (patch) => setCfg(prev => ({ ...(prev || {}), ...patch }));

  const save = async () => {
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      await apiFetch(`/api/guild/${guildId}/welcome-settings`, {
        method: "POST",
        body: JSON.stringify(cfg),
      });
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 1800);
      // Invalidate preview so next click regenerates with fresh settings
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
        setPreviewUrl(null);
      }
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setSaving(false);
    }
  };

  const loadPreview = async (which) => {
    setPreviewLoading(true);
    setPreviewWhich(which);
    setError(null);
    try {
      // Use raw fetch so we get the binary PNG, not parsed as JSON
      const resp = await fetch(
        `/api/guild/${guildId}/welcome-preview?action=${which}`,
        { credentials: "include" }
      );
      if (!resp.ok) {
        throw new Error(`Preview returned ${resp.status}`);
      }
      const blob = await resp.blob();
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewUrl(URL.createObjectURL(blob));
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setPreviewLoading(false);
    }
  };

  // Clean up blob URLs when component unmounts
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);

  if (loading) {
    return (
      <div style={{ ...C.card, marginTop:16, padding:16 }}>
        <div style={{ fontSize:13, color:"var(--text3)" }}>Loading welcome settings…</div>
      </div>
    );
  }

  if (!cfg) {
    return (
      <div style={{ ...C.card, marginTop:16, padding:16 }}>
        <div style={{ fontSize:13, color:"var(--red)", fontFamily:"'JetBrains Mono',monospace" }}>
          {error || "Failed to load welcome settings."}
        </div>
      </div>
    );
  }

  return (
    <div style={{ ...C.card, marginTop:16, padding:18 }}>
      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:14 }}>
        <div>
          <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:15, color:"var(--text)", textShadow:"0 0 10px rgba(0,245,212,0.3)" }}>
            👋 Welcome & Goodbye Banners
          </div>
          <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:3 }}>
            Auto-post a hue-shifted banner image when users join or leave. Banner color follows this server's embed color.
          </div>
        </div>
      </div>

      {error && (
        <div style={{ fontSize:12, color:"var(--red)", fontFamily:"'JetBrains Mono',monospace", marginBottom:10 }}>
          {error}
        </div>
      )}

      {/* Welcome row */}
      <div style={{ paddingTop:12, borderTop:"1px solid var(--border)" }}>
        <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:8 }}>
          <div style={{ fontSize:13, fontWeight:600, fontFamily:"'Outfit',sans-serif" }}>Welcome banner</div>
          <button
            onClick={() => update({ welcome_enabled: !cfg.welcome_enabled })}
            style={{ width:44, height:24, borderRadius:12, border:"none", cursor:"pointer", background: cfg.welcome_enabled ? "var(--cyan)" : "var(--border2)", position:"relative", transition:"background 0.2s", boxShadow: cfg.welcome_enabled ? "0 0 10px rgba(0,245,212,0.4)" : "none" }}
          >
            <div style={{ position:"absolute", top:3, left: cfg.welcome_enabled ? 22 : 3, width:18, height:18, borderRadius:"50%", background:"#fff", transition:"left 0.2s" }} />
          </button>
        </div>
        <div style={{ display:"flex", flexDirection:"column", gap:8, opacity: cfg.welcome_enabled ? 1 : 0.5 }}>
          <select
            value={cfg.welcome_channel_id || ""}
            onChange={(e) => update({ welcome_channel_id: e.target.value || null })}
            disabled={!cfg.welcome_enabled}
            style={{ background:"var(--bg2)", color:"var(--text)", border:"1px solid var(--border)", borderRadius:6, padding:"7px 10px", fontSize:13, fontFamily:"'JetBrains Mono',monospace" }}
          >
            <option value="">-- Select welcome channel --</option>
            {textChannels.map(c => (
              <option key={c.id} value={c.id}>#{c.name}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Goodbye row */}
      <div style={{ paddingTop:14, marginTop:14, borderTop:"1px solid var(--border)" }}>
        <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:8 }}>
          <div style={{ fontSize:13, fontWeight:600, fontFamily:"'Outfit',sans-serif" }}>Goodbye banner</div>
          <button
            onClick={() => update({ goodbye_enabled: !cfg.goodbye_enabled })}
            style={{ width:44, height:24, borderRadius:12, border:"none", cursor:"pointer", background: cfg.goodbye_enabled ? "var(--cyan)" : "var(--border2)", position:"relative", transition:"background 0.2s", boxShadow: cfg.goodbye_enabled ? "0 0 10px rgba(0,245,212,0.4)" : "none" }}
          >
            <div style={{ position:"absolute", top:3, left: cfg.goodbye_enabled ? 22 : 3, width:18, height:18, borderRadius:"50%", background:"#fff", transition:"left 0.2s" }} />
          </button>
        </div>
        <div style={{ display:"flex", flexDirection:"column", gap:8, opacity: cfg.goodbye_enabled ? 1 : 0.5 }}>
          <select
            value={cfg.goodbye_channel_id || ""}
            onChange={(e) => update({ goodbye_channel_id: e.target.value || null })}
            disabled={!cfg.goodbye_enabled}
            style={{ background:"var(--bg2)", color:"var(--text)", border:"1px solid var(--border)", borderRadius:6, padding:"7px 10px", fontSize:13, fontFamily:"'JetBrains Mono',monospace" }}
          >
            <option value="">-- Select goodbye channel --</option>
            {textChannels.map(c => (
              <option key={c.id} value={c.id}>#{c.name}</option>
            ))}
          </select>
          <div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>
            Kicks and bans are auto-suppressed via Discord audit log (requires View Audit Log).
          </div>
        </div>
      </div>

      {/* Save + Preview */}
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginTop:18, paddingTop:14, borderTop:"1px solid var(--border)" }}>
        <div style={{ display:"flex", gap:8 }}>
          <button onClick={() => loadPreview("welcome")} disabled={previewLoading}
            style={{ ...C.btnSecondary, fontSize:12, opacity: previewLoading ? 0.5 : 1 }}>
            {previewLoading && previewWhich === "welcome" ? "Generating…" : "Preview welcome"}
          </button>
          <button onClick={() => loadPreview("goodbye")} disabled={previewLoading}
            style={{ ...C.btnSecondary, fontSize:12, opacity: previewLoading ? 0.5 : 1 }}>
            {previewLoading && previewWhich === "goodbye" ? "Generating…" : "Preview goodbye"}
          </button>
        </div>
        <div style={{ display:"flex", gap:10, alignItems:"center" }}>
          {savedFlash && <span style={{ fontSize:11, color:"var(--green)", fontFamily:"'JetBrains Mono',monospace" }}>✓ Saved</span>}
          <button onClick={save} disabled={saving}
            style={{ ...C.btnPrimary, fontSize:13, opacity: saving ? 0.5 : 1 }}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {previewUrl && (
        <div style={{ marginTop:14, borderRadius:8, overflow:"hidden", border:"1px solid var(--border)" }}>
          <img src={previewUrl} alt={`${previewWhich} preview`} style={{ width:"100%", display:"block" }} />
          <div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", padding:"6px 10px", background:"var(--bg2)" }}>
            Preview uses your name and avatar. The real banner will use the joining/leaving user's info.
          </div>
        </div>
      )}
    </div>
  );
}

// ── General Settings Tab ──────────────────────────────────────────────────────
function GeneralSettingsTab({ guildId }) {
  const [settings, setSettings]       = useState(null);
  const [channels, setChannels]       = useState([]);
  const [roles, setRoles]             = useState([]);
  const [loading, setLoading]         = useState(true);
  const [saving, setSaving]           = useState({});
  const [showCreateRoleModal, setShowCreateRoleModal] = useState(false);
  const [createRoleForm, setCreateRoleForm] = useState({ name:"", color:"#5865f2" });
  const [showCreateLiveRoleModal, setShowCreateLiveRoleModal] = useState(false);
  const [createLiveRoleForm, setCreateLiveRoleForm] = useState({ name:"Live 🔴", color:"#e74c3c" });
  const [permIssues, setPermIssues]   = useState([]);
  const [rechecking, setRechecking]   = useState(false);
  const [fixing, setFixing]           = useState({});
  const [fixResults, setFixResults]   = useState({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, ch, rl, pi] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/settings`),
        apiFetch(`/api/guild/${guildId}/channels`),
        apiFetch(`/api/guild/${guildId}/roles`),
        apiFetch(`/api/guild/${guildId}/permission-issues`),
      ]);
      setSettings(s);
      setChannels(ch.channels || []);
      setRoles(rl || []);
      setPermIssues(pi || []);
    } catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const save = async (field, value) => {
    setSaving(p => ({ ...p, [field]: true }));
    try {
      await apiFetch(`/api/guild/${guildId}/settings`, { method:"PATCH", body: JSON.stringify({ [field]: value }) });
      setSettings(p => ({ ...p, [field]: value }));
    } catch(e) { alert("Failed to save: " + e.message); }
    finally { setSaving(p => ({ ...p, [field]: false })); }
  };

  const savePingRole = async (roleId) => {
    setSaving(p => ({ ...p, ping_role_id: true }));
    try {
      await apiFetch(`/api/guild/${guildId}/settings`, { method:"PATCH", body: JSON.stringify({ ping_role_id: roleId || null }) });
      setSettings(p => ({ ...p, ping_role_id: roleId || null }));
    } catch(e) { alert("Failed to save ping role: " + e.message); }
    finally { setSaving(p => ({ ...p, ping_role_id: false })); }
  };

  const saveCreateRole = async () => {
    if (!createRoleForm.name.trim()) { alert("Role name is required"); return; }
    setSaving(p => ({ ...p, ping_role_id: true }));
    try {
      const colorInt = parseInt(createRoleForm.color.replace("#",""), 16);
      const resp = await apiFetch(`/api/guild/${guildId}/settings`, {
        method:"PATCH",
        body: JSON.stringify({ ping_role_id:"__create__", new_role_name: createRoleForm.name.trim(), new_role_color: colorInt })
      });
      const [s, rl] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/settings`),
        apiFetch(`/api/guild/${guildId}/roles`),
      ]);
      setSettings(s);
      setRoles(rl || []);
      setShowCreateRoleModal(false);
      setCreateRoleForm({ name:"", color:"#5865f2" });
    } catch(e) { alert("Failed to create role: " + e.message); }
    finally { setSaving(p => ({ ...p, ping_role_id: false })); }
  };

  const saveLiveRole = async (roleId) => {
    setSaving(p => ({ ...p, live_role_id: true }));
    try {
      await apiFetch(`/api/guild/${guildId}/settings`, { method:"PATCH", body: JSON.stringify({ live_role_id: roleId || null }) });
      setSettings(p => ({ ...p, live_role_id: roleId || null }));
    } catch(e) { alert("Failed to save live role: " + e.message); }
    finally { setSaving(p => ({ ...p, live_role_id: false })); }
  };

  const saveCreateLiveRole = async () => {
    if (!createLiveRoleForm.name.trim()) { alert("Role name is required"); return; }
    setSaving(p => ({ ...p, live_role_id: true }));
    try {
      const colorInt = parseInt(createLiveRoleForm.color.replace("#",""), 16);
      await apiFetch(`/api/guild/${guildId}/settings`, { method:"PATCH",
        body: JSON.stringify({ live_role_id:"__create__", new_role_name: createLiveRoleForm.name.trim(), new_role_color: colorInt })
      });
      const [s, rl] = await Promise.all([apiFetch(`/api/guild/${guildId}/settings`), apiFetch(`/api/guild/${guildId}/roles`)]);
      setSettings(s); setRoles(rl || []);
      setShowCreateLiveRoleModal(false);
      setCreateLiveRoleForm({ name:"Live 🔴", color:"#e74c3c" });
    } catch(e) { alert("Failed to create role: " + e.message); }
    finally { setSaving(p => ({ ...p, live_role_id: false })); }
  };

  const recheckPerms = async () => {
    setRechecking(true);
    try {
      await apiFetch(`/api/guild/${guildId}/permission-issues/recheck`, { method:"POST" });
      // Give the bot a moment to run the check then reload
      await new Promise(r => setTimeout(r, 2500));
      const pi = await apiFetch(`/api/guild/${guildId}/permission-issues`);
      setPermIssues(pi || []);
    } catch(e) { console.error("Recheck failed:", e); }
    finally { setRechecking(false); }
  };

  const fixPerms = async (channelId) => {
    setFixing(p => ({ ...p, [channelId]: true }));
    setFixResults(p => ({ ...p, [channelId]: null }));
    try {
      const res = await apiFetch(`/api/guild/${guildId}/permission-issues/${channelId}/fix`, { method:"POST" });
      setFixResults(p => ({ ...p, [channelId]: res }));
      if (res.ok) {
        await new Promise(r => setTimeout(r, 1500));
        const pi = await apiFetch(`/api/guild/${guildId}/permission-issues`);
        setPermIssues(pi || []);
      }
    } catch(e) { setFixResults(p => ({ ...p, [channelId]: { ok: false, message: e.message } })); }
    finally { setFixing(p => ({ ...p, [channelId]: false })); }
  };

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  const toggle = (label, field, value) => (
    <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"14px 0", borderBottom:"1px solid var(--border)" }}>
      <span style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>{label}</span>
      <button
        onClick={() => save(field, !value)}
        disabled={saving[field]}
        style={{ width:44, height:24, borderRadius:12, border:"none", cursor:"pointer", background:value?"var(--cyan)":"var(--border2)", position:"relative", transition:"background 0.2s", flexShrink:0, boxShadow:value?"0 0 10px rgba(0,245,212,0.4)":"none" }}
      >
        <div style={{ position:"absolute", top:3, left:value?22:3, width:18, height:18, borderRadius:"50%", background:"#fff", transition:"left 0.2s" }} />
      </button>
    </div>
  );

  return (
    <div>
      <PageHeader title="General Settings" subtitle="Core notification, appearance, and role settings" />

      {/* ── Permission Issues Banner ── */}
      {permIssues.length > 0 && (
        <div style={{ marginBottom:16, padding:"14px 18px", borderRadius:10, background:"rgba(245,200,66,0.08)", border:"1px solid rgba(245,200,66,0.35)", display:"flex", gap:14, alignItems:"flex-start" }}>
          <div style={{ fontSize:20, flexShrink:0, marginTop:1 }}>⚠️</div>
          <div style={{ flex:1 }}>
            <div style={{ fontWeight:700, fontSize:14, color:"#f5c842", fontFamily:"'Orbitron',sans-serif", marginBottom:6 }}>
              Permission Issues Detected
            </div>
            <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
              {permIssues.map(p => (
                <div key={p.channel_id}>
                  <div style={{ display:"flex", alignItems:"center", gap:10, flexWrap:"wrap" }}>
                    <div style={{ fontSize:12, color:"#e2d88a", fontFamily:"'Outfit',sans-serif" }}>
                      <span style={{ color:"#f5c842", fontFamily:"'JetBrains Mono',monospace" }}>{p.channel_name}</span>
                      {" — missing: "}
                      <span style={{ color:"#ffd966" }}>{p.missing.join(", ")}</span>
                    </div>
                    <button
                      onClick={() => fixPerms(p.channel_id)}
                      disabled={fixing[p.channel_id]}
                      style={{ padding:"3px 10px", borderRadius:5, border:"1px solid rgba(245,200,66,0.4)", background:"rgba(245,200,66,0.12)", color:"#f5c842", fontSize:11, cursor:"pointer", fontFamily:"'Outfit',sans-serif", fontWeight:600, opacity: fixing[p.channel_id] ? 0.6 : 1, flexShrink:0 }}
                    >
                      {fixing[p.channel_id] ? "Fixing…" : "Attempt Auto-Fix"}
                    </button>
                  </div>
                  {fixResults[p.channel_id] && (
                    <div style={{ marginTop:4, fontSize:11, fontFamily:"'Outfit',sans-serif",
                      color: fixResults[p.channel_id].ok ? "var(--green)" : "#ffa07a",
                      padding:"5px 10px", borderRadius:5,
                      background: fixResults[p.channel_id].ok ? "rgba(57,217,138,0.08)" : "rgba(255,100,80,0.08)",
                      border: `1px solid ${fixResults[p.channel_id].ok ? "rgba(57,217,138,0.2)" : "rgba(255,100,80,0.2)"}` }}>
                      {fixResults[p.channel_id].ok ? "✓ " : "✗ "}{fixResults[p.channel_id].message}
                      {!fixResults[p.channel_id].ok && (
                        <div style={{ marginTop:6, color:"rgba(255,160,122,0.8)", lineHeight:1.5 }}>
                          <strong>To fix manually in Discord:</strong><br/>
                          1. Go to your server settings → Roles → find ExcelProtocol's role<br/>
                          2. Ensure it has <strong>Manage Roles</strong> or <strong>Manage Channels</strong> enabled<br/>
                          3. Or go to the channel → Edit Channel → Permissions → add ExcelProtocol with the missing permissions<br/>
                          4. Come back here and click Re-check
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
            {permIssues.some(p => p.missing.some(m => m.includes("Manage Roles"))) && (
              <div style={{ fontSize:11, color:"rgba(245,200,66,0.75)", marginTop:8, fontFamily:"'Outfit',sans-serif", padding:"6px 10px", borderRadius:6, background:"rgba(245,200,66,0.06)", border:"1px solid rgba(245,200,66,0.15)" }}>
                ℹ️ <strong>Manage Roles</strong> and <strong>Manage Channels</strong> are server-wide permissions — grant them in your Discord server's role settings, not per-channel.
              </div>
            )}
            <div style={{ fontSize:11, color:"rgba(245,200,66,0.55)", marginTop:8, fontFamily:"'JetBrains Mono',monospace" }}>
              Bot checks every 10 minutes. Fix permissions in Discord then re-check below.
            </div>
          </div>
          <button
            onClick={recheckPerms}
            disabled={rechecking}
            style={{ flexShrink:0, padding:"6px 14px", borderRadius:7, border:"1px solid rgba(245,200,66,0.4)", background:"rgba(245,200,66,0.1)", color:"#f5c842", fontSize:12, cursor:"pointer", fontFamily:"'Outfit',sans-serif", fontWeight:600, opacity: rechecking ? 0.6 : 1 }}
          >
            {rechecking ? "Checking…" : "Re-check"}
          </button>
        </div>
      )}

      {/* ── General ── */}
      <div style={{ ...C.card, marginBottom:16 }}>
        {sectionHead("General", "Core notification and appearance settings")}

        <div style={{ display:"flex", flexDirection:"column", gap:14 }}>
          <Field label="Default Notification Channel">
            <CyanSelect value={settings?.notification_channel_id || ""} onChange={e => save("notification_channel_id", e.target.value)}>
              <option value="">Select channel...</option>
              {channels.map(c => <option key={c.id} value={c.id}>#{c.name}</option>)}
            </CyanSelect>
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>Updates all streamers without a custom channel</div>
          </Field>

          <Field label="Embed Colour">
            <div style={{ display:"flex", alignItems:"center", gap:10 }}>
              <input type="color" value={settings?.embed_color || "#00ffff"}
                onChange={e => setSettings(p => ({ ...p, embed_color: e.target.value }))}
                onBlur={e => save("embed_color", e.target.value)}
                style={{ width:44, height:34, borderRadius:6, border:"1px solid var(--glass-border)", background:"none", cursor:"pointer", padding:2 }} />
              <span style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{settings?.embed_color || "#00ffff"}</span>
            </div>
          </Field>

          <Field label="Stream Notification Ping Role">
            <div style={{ display:"flex", gap:8, alignItems:"center" }}>
              <CyanSelect
                value={settings?.ping_role_id || ""}
                onChange={e => {
                  const v = e.target.value;
                  if (v === "__create__") {
                    setCreateRoleForm({ name:"", color:"#5865f2" });
                    setShowCreateRoleModal(true);
                  } else {
                    savePingRole(v);
                  }
                }}
                style={{ flex:1 }}
              >
                <option value="">No ping</option>
                <option value="__create__">➕ Create new role…</option>
                {roles.map(r => (
                  <option key={r.id} value={r.id}>@{r.name}</option>
                ))}
              </CyanSelect>
              {settings?.ping_role_id && (
                <button
                  onClick={() => savePingRole(null)}
                  disabled={saving.ping_role_id}
                  style={{ ...C.btnDanger, padding:"6px 12px", fontSize:12, flexShrink:0 }}
                >Clear</button>
              )}
            </div>
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>
              Role mentioned in every stream notification
            </div>
          </Field>

          <Field label="Live Role">
            <div style={{ display:"flex", gap:8, alignItems:"center" }}>
              <CyanSelect
                value={settings?.live_role_id || ""}
                onChange={e => {
                  const v = e.target.value;
                  if (v === "__create__") {
                    setShowCreateLiveRoleModal(true);
                  } else {
                    saveLiveRole(v);
                  }
                }}
                style={{ flex:1 }}
              >
                <option value="">No live role</option>
                <option value="__create__">➕ Create new role…</option>
                {roles.map(r => (
                  <option key={r.id} value={r.id}>@{r.name}</option>
                ))}
              </CyanSelect>
              {settings?.live_role_id && (
                <button onClick={() => saveLiveRole(null)} disabled={saving.live_role_id}
                  style={{ ...C.btnDanger, padding:"6px 12px", fontSize:12, flexShrink:0 }}>Clear</button>
              )}
            </div>
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>
              Assigned to streamers when they go live, removed when offline
            </div>
            <div style={{ marginTop:8, padding:"8px 10px", borderRadius:6, background:"rgba(255,200,0,0.06)", border:"1px solid rgba(255,200,0,0.2)" }}>
              <div style={{ fontSize:11, color:"rgba(255,200,0,0.85)", fontFamily:"'JetBrains Mono',monospace", lineHeight:1.6 }}>
                <span style={{ fontWeight:700 }}>⚠ Setup checklist</span><br />
                1. Place the Live role <strong>as high as possible</strong> in Server Settings → Roles (roles only affect members below them in the hierarchy).<br />
                2. In the role settings, enable <strong>Display role members separately from online members</strong> so viewers appear in their own section while live.<br />
                3. The bot&apos;s own role must be <strong>above the Live role</strong> in the hierarchy — otherwise Discord will reject the role assignment.
              </div>
            </div>
          </Field>
        </div>

        <div style={{ marginTop:16 }}>
          {toggle("Auto-delete stream notifications when streamer goes offline", "auto_delete_notifications", settings?.auto_delete_notifications)}
          {toggle("Milestone notifications (5h, 10h stream alerts)", "milestone_notifications", settings?.milestone_notifications)}
        </div>
      </div>

      {/* ── Create Ping Role Modal ── */}
      {showCreateRoleModal && (
        <Modal onClose={() => setShowCreateRoleModal(false)} width={400}>
          <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>
            Create Ping Role
          </div>
          <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", marginTop:-8 }}>
            A new role will be created in your Discord server and set as the ping role.
          </div>

          <Field label="Role Name">
            <CyanInput
              value={createRoleForm.name}
              onChange={e => setCreateRoleForm(p => ({ ...p, name: e.target.value }))}
              placeholder="e.g. Stream Ping"
              autoFocus
            />
          </Field>

          <Field label="Role Colour">
            <div style={{ display:"flex", alignItems:"center", gap:10 }}>
              <input
                type="color"
                value={createRoleForm.color}
                onChange={e => setCreateRoleForm(p => ({ ...p, color: e.target.value }))}
                style={{ width:44, height:34, borderRadius:6, border:"1px solid var(--glass-border)", background:"none", cursor:"pointer", padding:2 }}
              />
              <span style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{createRoleForm.color}</span>
            </div>
          </Field>

          <div style={{ display:"flex", justifyContent:"flex-end", gap:10 }}>
            <button onClick={() => setShowCreateRoleModal(false)} style={C.btnSecondary}>Cancel</button>
            <button onClick={saveCreateRole} disabled={saving.ping_role_id} style={C.btnPrimary}>
              {saving.ping_role_id ? "Creating…" : "Create Role"}
            </button>
          </div>
        </Modal>
      )}

      {/* ── Create Live Role Modal ── */}
      {showCreateLiveRoleModal && (
        <Modal onClose={() => setShowCreateLiveRoleModal(false)} width={400}>
          <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>Create Live Role</div>
          <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", marginTop:-8 }}>
            A new role will be created and assigned to streamers when they go live.
          </div>
          <Field label="Role Name">
            <CyanInput value={createLiveRoleForm.name} onChange={e => setCreateLiveRoleForm(p => ({ ...p, name: e.target.value }))} placeholder="e.g. Live 🔴" autoFocus />
          </Field>
          <Field label="Role Colour">
            <div style={{ display:"flex", alignItems:"center", gap:10 }}>
              <input type="color" value={createLiveRoleForm.color} onChange={e => setCreateLiveRoleForm(p => ({ ...p, color: e.target.value }))}
                style={{ width:44, height:34, borderRadius:6, border:"1px solid var(--glass-border)", background:"none", cursor:"pointer", padding:2 }} />
              <span style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{createLiveRoleForm.color}</span>
            </div>
          </Field>
          <div style={{ display:"flex", justifyContent:"flex-end", gap:10 }}>
            <button onClick={() => setShowCreateLiveRoleModal(false)} style={C.btnSecondary}>Cancel</button>
            <button onClick={saveCreateLiveRole} disabled={saving.live_role_id} style={C.btnPrimary}>
              {saving.live_role_id ? "Creating…" : "Create Role"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}


// ── Birthdays Tab ─────────────────────────────────────────────────────────────
function BirthdaysTab({ guildId }) {
  const [settings, setSettings]   = useState(null);
  const [channels, setChannels]   = useState([]);
  const [birthdays, setBirthdays] = useState([]);
  const [members, setMembers]     = useState([]);
  const [loading, setLoading]     = useState(true);
  const [saving, setSaving]       = useState({});
  const [showBdayModal, setShowBdayModal] = useState(false);
  const [bdayForm, setBdayForm]   = useState({ user_id:"", day:1, month:1, year:"" });
  const [bdayEditId, setBdayEditId] = useState(null);

  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, ch, bd, mb] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/settings`),
        apiFetch(`/api/guild/${guildId}/channels`),
        apiFetch(`/api/guild/${guildId}/birthdays`),
        apiFetch(`/api/guild/${guildId}/members`),
      ]);
      setSettings(s);
      setChannels(ch.channels || []);
      setBirthdays(bd);
      setMembers(Array.isArray(mb) ? mb : (mb?.members || []));
    } catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const save = async (field, value) => {
    setSaving(p => ({ ...p, [field]: true }));
    try {
      await apiFetch(`/api/guild/${guildId}/settings`, { method:"PATCH", body: JSON.stringify({ [field]: value }) });
      setSettings(p => ({ ...p, [field]: value }));
    } catch(e) { alert("Failed to save: " + e.message); }
    finally { setSaving(p => ({ ...p, [field]: false })); }
  };

  const openAddBday = () => {
    setBdayEditId(null);
    setBdayForm({ user_id: members[0]?.id || "", day:1, month:1, year:"" });
    setShowBdayModal(true);
  };
  const openEditBday = (b) => {
    setBdayEditId(b.user_id);
    setBdayForm({ user_id: b.user_id, day: b.day, month: b.month, year: b.year || "" });
    setShowBdayModal(true);
  };
  const saveBday = async () => {
    try {
      await apiFetch(`/api/guild/${guildId}/birthdays`, {
        method:"POST",
        body: JSON.stringify({ user_id: bdayForm.user_id, day: Number(bdayForm.day), month: Number(bdayForm.month), year: bdayForm.year ? Number(bdayForm.year) : 0 })
      });
      setShowBdayModal(false);
      load();
    } catch(e) { alert("Failed: " + e.message); }
  };
  const deleteBday = async (userId) => {
    if (!confirm("Remove this birthday?")) return;
    try {
      await apiFetch(`/api/guild/${guildId}/birthdays/${userId}`, { method:"DELETE" });
      load();
    } catch(e) { alert("Failed: " + e.message); }
  };

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  return (
    <div>
      <PageHeader title="Birthdays" subtitle="Announcement channel and member birthdays" />

      <div style={{ ...C.card, marginBottom:16 }}>
        <Field label="Birthday Announcement Channel">
          <CyanSelect value={settings?.birthday_channel_id || ""} onChange={e => save("birthday_channel_id", e.target.value)}>
            <option value="">Not set</option>
            {channels.map(c => <option key={c.id} value={c.id}>#{c.name}</option>)}
          </CyanSelect>
        </Field>
      </div>

      <div style={{ ...C.card }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:18 }}>
          {sectionHead("Registered Birthdays", `${birthdays.length} member${birthdays.length !== 1 ? "s" : ""}`)}
          <button onClick={openAddBday} style={{ ...C.btnPrimary, flexShrink:0 }}>+ Add Birthday</button>
        </div>

        {birthdays.length > 0 && (
          <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
            {birthdays.map(b => (
              <div key={b.user_id} style={{ display:"flex", alignItems:"center", gap:12, padding:"10px 14px", borderRadius:8, background:"rgba(8,11,15,0.6)", border:"1px solid var(--border)" }}>
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:13, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>{b.username}</div>
                  <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>
                    {MONTHS[b.month-1]} {b.day}{b.year ? `, ${b.year}` : ""}
                  </div>
                </div>
                <button onClick={() => openEditBday(b)} style={{ ...C.btnSecondary, padding:"5px 12px", fontSize:12 }}>Edit</button>
                <button onClick={() => deleteBday(b.user_id)} style={{ ...C.btnDanger }}>Remove</button>
              </div>
            ))}
          </div>
        )}
        {birthdays.length === 0 && (
          <div style={{ textAlign:"center", padding:"24px 0 8px", color:"var(--text3)" }}>
            <div style={{ fontSize:24, marginBottom:6 }}>🎂</div>
            <div style={{ fontSize:13, fontFamily:"'Outfit',sans-serif" }}>No birthdays registered yet</div>
          </div>
        )}
      </div>

      {showBdayModal && (
        <Modal onClose={() => setShowBdayModal(false)} width={400}>
          <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>
            {bdayEditId ? "Edit Birthday" : "Add Birthday"}
          </div>
          {!bdayEditId && (
            <Field label="Member">
              <CyanSelect value={bdayForm.user_id} onChange={e => setBdayForm(p => ({ ...p, user_id: e.target.value }))}>
                <option value="">Select member...</option>
                {members.map(m => <option key={m.id} value={m.id}>{m.username}</option>)}
              </CyanSelect>
            </Field>
          )}
          {bdayEditId && (
            <div style={{ fontSize:13, color:"var(--text2)", fontFamily:"'Outfit',sans-serif" }}>
              Editing: <span style={{ color:"var(--cyan)" }}>{birthdays.find(b=>b.user_id===bdayEditId)?.username}</span>
            </div>
          )}
          <div style={{ display:"flex", gap:10 }}>
            <Field label="Day">
              <CyanInput type="number" min={1} max={31} value={bdayForm.day}
                onChange={e => setBdayForm(p => ({ ...p, day: e.target.value }))} style={{ width:80 }} />
            </Field>
            <Field label="Month">
              <CyanSelect value={bdayForm.month} onChange={e => setBdayForm(p => ({ ...p, month: e.target.value }))}>
                {MONTHS.map((m,i) => <option key={i+1} value={i+1}>{m}</option>)}
              </CyanSelect>
            </Field>
            <Field label="Year (optional)">
              <CyanInput type="number" min={1900} max={new Date().getFullYear()} value={bdayForm.year}
                onChange={e => setBdayForm(p => ({ ...p, year: e.target.value }))} style={{ width:90 }} placeholder="—" />
            </Field>
          </div>
          <div style={{ display:"flex", justifyContent:"flex-end", gap:10 }}>
            <button onClick={() => setShowBdayModal(false)} style={C.btnSecondary}>Cancel</button>
            <button onClick={saveBday} style={C.btnPrimary}>{bdayEditId ? "Save Changes" : "Add Birthday"}</button>
          </div>
        </Modal>
      )}
    </div>
  );
}


// ── Welcome & Goodbye Tab ─────────────────────────────────────────────────────
function WelcomeGoodbyeTab({ guildId }) {
  const [channels, setChannels] = useState([]);
  const [loading, setLoading]   = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const ch = await apiFetch(`/api/guild/${guildId}/channels`);
      setChannels(ch.channels || []);
    } catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  return (
    <div>
      <PageHeader title="Welcome & Goodbye" subtitle="Auto-post banner images when members join or leave" />
      <WelcomeSettings guildId={guildId} channels={channels} />
    </div>
  );
}


// ── Voice Rooms Tab ───────────────────────────────────────────────────────────
function VoiceRoomsTab({ guildId }) {
  const [voiceChannels, setVoiceChannels] = useState([]);
  const [vcSettings, setVcSettings]       = useState(null);
  const [vcForm, setVcForm]               = useState({ trigger_channel_id:"", name_template:"{username}'s VC" });
  const [savingVc, setSavingVc]           = useState(false);
  const [loading, setLoading]             = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [ch, vc] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/channels`),
        apiFetch(`/api/guild/${guildId}/vc-settings`).catch(() => ({ enabled: false })),
      ]);
      setVoiceChannels(ch.voice_channels || []);
      setVcSettings(vc);
    } catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  return (
    <div>
      <PageHeader title="Voice Rooms" subtitle="Auto-create personal voice channels when members join a trigger channel" />

      <div style={{ ...C.card }}>
        {sectionHead("Trigger Setup", "Members who join the trigger channel get their own voice room")}
        <div style={{ display:"flex", flexDirection:"column", gap:14 }}>
          <Field label="Trigger Channel (Voice)">
            <CyanSelect value={vcForm.trigger_channel_id} onChange={e => setVcForm(p => ({ ...p, trigger_channel_id: e.target.value }))}>
              <option value="">Select voice channel...</option>
              {voiceChannels.map(c => <option key={c.id} value={c.id}>🔊 {c.name}</option>)}
            </CyanSelect>
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>
              Members who join this channel will get their own voice room created automatically
            </div>
          </Field>
          <Field label="Room Name Template">
            <CyanInput value={vcForm.name_template} onChange={e => setVcForm(p => ({ ...p, name_template: e.target.value }))} placeholder="{username}'s VC" />
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>
              Use <span style={{ color:"var(--cyan)" }}>{"{username}"}</span> as a placeholder for the member&apos;s display name
            </div>
          </Field>
          <div>
            <button
              onClick={async () => {
                if (!vcForm.trigger_channel_id) return;
                setSavingVc(true);
                try {
                  await apiFetch(`/api/guild/${guildId}/vc-settings`, { method:"POST", body: JSON.stringify(vcForm) });
                  const updated = await apiFetch(`/api/guild/${guildId}/vc-settings`);
                  setVcSettings(updated);
                  setVcForm({ trigger_channel_id:"", name_template:"{username}'s VC" });
                } catch(e) { alert("Failed: " + e.message); }
                setSavingVc(false);
              }}
              disabled={savingVc || !vcForm.trigger_channel_id}
              style={{ ...C.btnPrimary, opacity: savingVc || !vcForm.trigger_channel_id ? 0.5 : 1 }}
            >
              {savingVc ? "Saving..." : "Add Trigger"}
            </button>
          </div>
          {(vcSettings?.configs || []).length > 0 && (
            <div style={{ display:"flex", flexDirection:"column", gap:8, marginTop:4 }}>
              <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", letterSpacing:1, textTransform:"uppercase" }}>Configured Triggers</div>
              {vcSettings.configs.map(cfg => (
                <div key={cfg.trigger_channel_id} style={{ display:"flex", alignItems:"center", gap:10, padding:"8px 12px", borderRadius:7, background:"rgba(0,245,212,0.04)", border:"1px solid rgba(0,245,212,0.12)" }}>
                  <span style={{ fontSize:14 }}>🔊</span>
                  <div style={{ flex:1 }}>
                    <div style={{ fontSize:13, color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>#{cfg.trigger_channel_name}</div>
                    <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>{cfg.name_template}</div>
                  </div>
                  <button
                    onClick={async () => {
                      if (!confirm(`Remove trigger #${cfg.trigger_channel_name}?`)) return;
                      await apiFetch(`/api/guild/${guildId}/vc-settings/${cfg.trigger_channel_id}`, { method:"DELETE" });
                      const updated = await apiFetch(`/api/guild/${guildId}/vc-settings`);
                      setVcSettings(updated);
                    }}
                    style={{ ...C.btnDanger, padding:"4px 10px", fontSize:11 }}
                  >Remove</button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


// ── Cleanup Rules Tab ─────────────────────────────────────────────────────────
function CleanupRulesTab({ guildId }) {
  const [channels, setChannels]           = useState([]);
  const [cleanup, setCleanup]             = useState([]);
  const [loading, setLoading]             = useState(true);
  const [showCleanupModal, setShowCleanupModal] = useState(false);
  const [editingCleanup, setEditingCleanup]     = useState(null);
  const [cleanupForm, setCleanupForm]     = useState({ channel_id:"", interval_hours:24, keep_pinned:true });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [ch, cl] = await Promise.all([
        apiFetch(`/api/guild/${guildId}/channels`),
        apiFetch(`/api/guild/${guildId}/cleanup`),
      ]);
      setChannels(ch.channels || []);
      setCleanup(cl);
    } catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const openAddCleanup = () => {
    setEditingCleanup(null);
    setCleanupForm({ channel_id: channels[0]?.id || "", interval_hours: 24, keep_pinned: true });
    setShowCleanupModal(true);
  };
  const openEditCleanup = (c) => {
    setEditingCleanup(c);
    setCleanupForm({ channel_id: c.channel_id, interval_hours: c.interval_hours, keep_pinned: c.keep_pinned });
    setShowCleanupModal(true);
  };
  const saveCleanup = async () => {
    try {
      if (editingCleanup) {
        await apiFetch(`/api/guild/${guildId}/cleanup/${editingCleanup.channel_id}`, {
          method:"PATCH", body: JSON.stringify({ interval_hours: cleanupForm.interval_hours, keep_pinned: cleanupForm.keep_pinned })
        });
      } else {
        await apiFetch(`/api/guild/${guildId}/cleanup`, {
          method:"POST", body: JSON.stringify(cleanupForm)
        });
      }
      setShowCleanupModal(false);
      load();
    } catch(e) { alert("Failed: " + e.message); }
  };
  const deleteCleanup = async (channelId) => {
    if (!confirm("Remove cleanup for this channel?")) return;
    try {
      await apiFetch(`/api/guild/${guildId}/cleanup/${channelId}`, { method:"DELETE" });
      load();
    } catch(e) { alert("Failed: " + e.message); }
  };

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  return (
    <div>
      <PageHeader title="Cleanup Rules" subtitle="Auto-delete messages in selected channels after a set time" />

      <div style={{ ...C.card }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:18 }}>
          {sectionHead("Configured Channels", "Messages older than the interval are automatically deleted")}
          <button onClick={openAddCleanup} style={{ ...C.btnPrimary, flexShrink:0 }}>+ Add Channel</button>
        </div>

        {cleanup.length === 0 ? (
          <div style={{ textAlign:"center", padding:"32px 0", color:"var(--text3)" }}>
            <div style={{ fontSize:28, marginBottom:8 }}>🧹</div>
            <div style={{ fontSize:13, fontFamily:"'Outfit',sans-serif" }}>No cleanup rules configured</div>
          </div>
        ) : (
          <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
            {cleanup.map(c => (
              <div key={c.channel_id} style={{ display:"flex", alignItems:"center", gap:12, padding:"12px 14px", borderRadius:8, background:"rgba(8,11,15,0.6)", border:"1px solid var(--border)" }}>
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:13, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>{c.channel_name}</div>
                  <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>
                    Every {c.interval_hours}h · {c.keep_pinned ? "keep pinned" : "delete pinned too"}
                  </div>
                </div>
                <button onClick={() => openEditCleanup(c)} style={{ ...C.btnSecondary, padding:"5px 12px", fontSize:12 }}>Edit</button>
                <button onClick={() => deleteCleanup(c.channel_id)} style={{ ...C.btnDanger }}>Remove</button>
              </div>
            ))}
          </div>
        )}
      </div>

      {showCleanupModal && (
        <Modal onClose={() => setShowCleanupModal(false)} width={420}>
          <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>
            {editingCleanup ? "Edit Cleanup Rule" : "Add Cleanup Rule"}
          </div>
          {!editingCleanup && (
            <Field label="Channel">
              <CyanSelect value={cleanupForm.channel_id} onChange={e => setCleanupForm(p => ({ ...p, channel_id: e.target.value }))}>
                <option value="">Select channel...</option>
                {channels.map(c => <option key={c.id} value={c.id}>#{c.name}</option>)}
              </CyanSelect>
            </Field>
          )}
          {editingCleanup && (
            <div style={{ fontSize:13, color:"var(--text2)", fontFamily:"'Outfit',sans-serif" }}>
              Editing: <span style={{ color:"var(--cyan)" }}>{editingCleanup.channel_name}</span>
            </div>
          )}
          <Field label="Delete messages older than (hours)">
            <CyanInput type="number" min={1} max={8760} value={cleanupForm.interval_hours}
              onChange={e => setCleanupForm(p => ({ ...p, interval_hours: parseInt(e.target.value) || 24 })) } />
          </Field>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"12px 0", borderTop:"1px solid var(--border)" }}>
            <span style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>Keep pinned messages</span>
            <button
              onClick={() => setCleanupForm(p => ({ ...p, keep_pinned: !p.keep_pinned }))}
              style={{ width:44, height:24, borderRadius:12, border:"none", cursor:"pointer", background:cleanupForm.keep_pinned?"var(--cyan)":"var(--border2)", position:"relative", transition:"background 0.2s", flexShrink:0, boxShadow:cleanupForm.keep_pinned?"0 0 10px rgba(0,245,212,0.4)":"none" }}
            >
              <div style={{ position:"absolute", top:3, left:cleanupForm.keep_pinned?22:3, width:18, height:18, borderRadius:"50%", background:"#fff", transition:"left 0.2s" }} />
            </button>
          </div>
          <div style={{ display:"flex", justifyContent:"flex-end", gap:10 }}>
            <button onClick={() => setShowCleanupModal(false)} style={C.btnSecondary}>Cancel</button>
            <button onClick={saveCleanup} style={C.btnPrimary}>{editingCleanup ? "Save Changes" : "Add Rule"}</button>
          </div>
        </Modal>
      )}
    </div>
  );
}


// ── Twitch Tab ────────────────────────────────────────────────────────────────
function TwitchTab({ guildId, isDev }) {
  const [info, setInfo]           = useState(null);
  const [loading, setLoading]     = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editing, setEditing]     = useState(null);
  const [editLimit, setEditLimit] = useState(false);
  const [newLimit, setNewLimit]   = useState(50);
  const [savingLimit, setSavingLimit] = useState(false);
  const [form, setForm] = useState({ command_name:"", response:"", permission:"everyone", cooldown_seconds:0 });
  const [saving, setSaving]       = useState(false);
  const [savingPlay, setSavingPlay] = useState(false);
  const [err, setErr]             = useState(null);

  const PERMS = ["everyone","subscriber","mod","broadcaster"];
  const BUILTIN = [
    { command_name:"!uptime",   description:"How long the stream has been live",  permission:"everyone", cooldown_seconds:30 },
    { command_name:"!game",     description:"Current game being played",          permission:"everyone", cooldown_seconds:30 },
    { command_name:"!title",    description:"Current stream title",               permission:"everyone", cooldown_seconds:30 },
    { command_name:"!viewers",  description:"Current viewer count",               permission:"everyone", cooldown_seconds:60 },
    { command_name:"!so",       description:"Shoutout another streamer",          permission:"mod",      cooldown_seconds:0  },
    { command_name:"!commands", description:"Lists all available commands",       permission:"everyone", cooldown_seconds:60 },
  ];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch(`/api/guild/${guildId}/twitch`);
      setInfo(data);
      setNewLimit(data.limit || 50);
    } catch(e) { console.error(e); }
    setLoading(false);
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const openAdd = () => {
    setEditing(null);
    setForm({ command_name:"", response:"", permission:"everyone", cooldown_seconds:0 });
    setErr(null);
    setShowModal(true);
  };

  const openEdit = (cmd) => {
    setEditing(cmd);
    setForm({ command_name: cmd.command_name, response: cmd.response, permission: cmd.permission, cooldown_seconds: cmd.cooldown_seconds });
    setErr(null);
    setShowModal(true);
  };

  const saveCmd = async () => {
    setSaving(true); setErr(null);
    try {
      await apiFetch(`/api/guild/${guildId}/twitch/commands`, { method:"POST", body: JSON.stringify({ ...form, cooldown_seconds: Number(form.cooldown_seconds) }) });
      setShowModal(false); load();
    } catch(e) { setErr(e.message); }
    setSaving(false);
  };

  const deleteCmd = async (name) => {
    if (!confirm(`Remove ${name}?`)) return;
    try { await apiFetch(`/api/guild/${guildId}/twitch/commands/${encodeURIComponent(name)}`, { method:"DELETE" }); load(); }
    catch(e) { alert("Failed: " + e.message); }
  };

  const saveLimit = async () => {
    setSavingLimit(true);
    try {
      await apiFetch(`/api/guild/${guildId}/command-limit`, { method:"PATCH", body: JSON.stringify({ limit: parseInt(newLimit) }) });
      setInfo(p => ({ ...p, limit: parseInt(newLimit) }));
      setEditLimit(false);
    } catch(e) { alert("Failed: " + e.message); }
    setSavingLimit(false);
  };

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  if (!info?.linked) return (
    <div>
      <PageHeader title="Twitch" subtitle="Twitch chat integration" />
      <div style={{ ...C.card, textAlign:"center", padding:"48px 0" }}>
        <div style={{ fontSize:32, marginBottom:12 }}>🟣</div>
        <div style={{ fontWeight:700, fontSize:15, fontFamily:"'Orbitron',sans-serif", color:"var(--text)", marginBottom:8 }}>No Twitch channel linked</div>
        <div style={{ fontSize:13, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", marginBottom:16 }}>
          Connect your Twitch account to enable chat commands and channel point rewards.
        </div>
        <a href={`/auth/twitch/login/${guildId}`}
          style={{ ...C.btnPrimary, display:"inline-flex", alignItems:"center", gap:8, textDecoration:"none" }}>
          🟣 Connect Twitch Account
        </a>
      </div>
    </div>
  );

  const atLimit = info.count >= info.limit;
  const limitColor = info.count >= info.limit ? "var(--red)" : info.count >= info.limit * 0.8 ? "var(--yellow)" : "var(--text3)";

  return (
    <div>
      <PageHeader title="Twitch"
        subtitle={
          <span style={{ display:"flex", alignItems:"center", gap:8, flexWrap:"wrap" }}>
            <span style={{ color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>#{info.channel}</span>
            <span style={{ color:"var(--text3)" }}>·</span>
            <span style={{ color:limitColor, fontFamily:"'JetBrains Mono',monospace" }}>{info.count}/{info.limit}</span>
            <span style={{ color:"var(--text3)" }}>custom commands</span>
            {isDev && !editLimit && <button onClick={()=>setEditLimit(true)} style={{ fontSize:10, padding:"1px 7px", borderRadius:4, border:"1px solid var(--border2)", background:"transparent", color:"var(--text3)", cursor:"pointer", fontFamily:"'JetBrains Mono',monospace" }}>edit limit</button>}
            {isDev && editLimit && (
              <span style={{ display:"inline-flex", alignItems:"center", gap:6 }}>
                <input type="number" min={1} value={newLimit} onChange={e=>setNewLimit(e.target.value)}
                  style={{ width:60, padding:"1px 6px", borderRadius:4, border:"1px solid var(--cyan)", background:"var(--bg1)", color:"var(--text)", fontSize:12, fontFamily:"'JetBrains Mono',monospace" }} />
                <button onClick={saveLimit} disabled={savingLimit} style={{ fontSize:10, padding:"2px 8px", borderRadius:4, border:"1px solid var(--cyan)", background:"var(--cyan-dim)", color:"var(--cyan)", cursor:"pointer" }}>{savingLimit?"...":"save"}</button>
                <button onClick={()=>setEditLimit(false)} style={{ fontSize:10, padding:"2px 8px", borderRadius:4, border:"1px solid var(--border2)", background:"transparent", color:"var(--text3)", cursor:"pointer" }}>cancel</button>
              </span>
            )}
          </span>
        }
        action={<button onClick={openAdd} disabled={atLimit && !isDev} style={{ ...C.btnPrimary, opacity:atLimit&&!isDev?0.4:1, cursor:atLimit&&!isDev?"not-allowed":"pointer" }}>+ Add Command</button>}
      />

      {atLimit && !isDev && <div style={{ marginBottom:12, padding:"10px 14px", borderRadius:8, border:"1px solid rgba(255,77,109,0.3)", background:"var(--red-dim)", color:"var(--red)", fontSize:12, fontFamily:"'Outfit',sans-serif" }}>⚠️ Command limit reached ({info.count}/{info.limit}). Contact the bot owner to increase your limit.</div>}
      <div style={{ marginBottom:12, padding:"10px 14px", borderRadius:8, border:"1px solid var(--border2)", background:"rgba(8,11,15,0.6)", color:"var(--text3)", fontSize:12, fontFamily:"'Outfit',sans-serif", display:"flex", alignItems:"center", gap:8 }}>💡 Make sure ExcelProtocol is modded in your Twitch chat — type <code style={{ fontFamily:"'JetBrains Mono',monospace", color:"var(--cyan)", background:"rgba(0,0,0,0.3)", padding:"1px 5px", borderRadius:3 }}>/mod ExcelProtocol</code> if you haven't already.</div>

      {/* Custom commands */}
      <div style={{ ...C.card, marginBottom:16 }}>
        <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", marginBottom:14, textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>Custom Commands</div>
        {info.commands.length === 0 ? (
          <div style={{ textAlign:"center", padding:"28px 0", color:"var(--text3)" }}>
            <div style={{ fontSize:24, marginBottom:6 }}>💬</div>
            <div style={{ fontSize:13, fontFamily:"'Outfit',sans-serif" }}>No custom commands yet</div>
          </div>
        ) : (
          <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
            {info.commands.map(cmd => (
              <div key={cmd.command_name} style={{ display:"flex", alignItems:"center", gap:12, padding:"11px 14px", borderRadius:8, background:"rgba(8,11,15,0.6)", border:"1px solid var(--border)" }}>
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{ display:"flex", alignItems:"center", gap:10, marginBottom:3 }}>
                    <span style={{ fontWeight:700, fontSize:13, fontFamily:"'JetBrains Mono',monospace", color:"var(--cyan)" }}>{cmd.command_name}</span>
                    <Badge text={cmd.permission} color={cmd.permission==="everyone"?"var(--green)":cmd.permission==="mod"?"var(--yellow)":cmd.permission==="broadcaster"?"var(--red)":"var(--cyan2)"} />
                    {cmd.cooldown_seconds > 0 && <span style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>⏱ {cmd.cooldown_seconds}s</span>}
                    {cmd.use_count > 0 && <span style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>×{cmd.use_count}</span>}
                  </div>
                  <div style={{ fontSize:12, color:"var(--text2)", fontFamily:"'Outfit',sans-serif", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{cmd.response}</div>
                </div>
                <button onClick={()=>openEdit(cmd)} style={{ ...C.btnSecondary, padding:"5px 12px", fontSize:12, flexShrink:0 }}>Edit</button>
                <button onClick={()=>deleteCmd(cmd.command_name)} style={{ ...C.btnDanger, flexShrink:0 }}>Remove</button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* !play command toggle */}
      <div style={{ ...C.card, marginBottom:16 }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
          <div>
            <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--text2)", marginBottom:4 }}>
              !play Command
            </div>
            <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif" }}>
              Mods can type <code style={{ color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>!play &lt;youtube_url&gt;</code> to trigger the OBS overlay — works without affiliate
            </div>
          </div>
          <button
            onClick={async () => {
              setSavingPlay(true);
              try {
                const newVal = !info.play_enabled;
                await apiFetch(`/api/guild/${guildId}/twitch/play-enabled`, { method:"POST", body: JSON.stringify({ enabled: newVal }) });
                setInfo(p => ({ ...p, play_enabled: newVal }));
              } catch(e) { alert("Failed: " + e.message); }
              setSavingPlay(false);
            }}
            disabled={savingPlay}
            style={{ width:44, height:24, borderRadius:12, border:"none", cursor:"pointer",
              background: info.play_enabled ? "var(--cyan)" : "var(--border2)", position:"relative",
              transition:"background 0.2s", flexShrink:0,
              boxShadow: info.play_enabled ? "0 0 10px rgba(0,245,212,0.4)" : "none",
              opacity: savingPlay ? 0.6 : 1 }}>
            <div style={{ position:"absolute", top:3, left: info.play_enabled ? 22 : 3, width:18, height:18,
              borderRadius:"50%", background:"#fff", transition:"left 0.2s" }} />
          </button>
        </div>
      </div>

      {/* Built-in commands */}
      <div style={{ ...C.card }}>
        <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--text2)", marginBottom:14 }}>Built-in Commands <span style={{ fontSize:11, fontWeight:400, color:"var(--text3)", fontFamily:"'Outfit',sans-serif" }}>(read-only)</span></div>
        <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
          {BUILTIN.map(cmd => (
            <div key={cmd.command_name} style={{ display:"flex", alignItems:"center", gap:10, padding:"9px 14px", borderRadius:8, background:"rgba(8,11,15,0.4)", border:"1px solid var(--border)" }}>
              <span style={{ fontWeight:700, fontSize:13, fontFamily:"'JetBrains Mono',monospace", color:"var(--text3)", width:100, flexShrink:0 }}>{cmd.command_name}</span>
              <span style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", flex:1 }}>{cmd.description}</span>
              <Badge text={cmd.permission} color={cmd.permission==="everyone"?"var(--green)":cmd.permission==="mod"?"var(--yellow)":"var(--red)"} />
              {cmd.cooldown_seconds > 0 && <span style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>⏱ {cmd.cooldown_seconds}s</span>}
            </div>
          ))}
        </div>
      </div>

      {/* Add/Edit Modal */}
      {showModal && (
        <Modal onClose={()=>setShowModal(false)} width={480}>
          <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>
            {editing ? `Edit ${editing.command_name}` : "Add Command"}
          </div>

          <Field label="Command Name">
            <CyanInput value={form.command_name} onChange={e=>setForm(p=>({...p, command_name:e.target.value}))}
              placeholder="!mycommand" disabled={!!editing}
              style={{ opacity:editing?0.5:1 }} />
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:3, fontFamily:"'JetBrains Mono',monospace" }}>! prefix added automatically if missing</div>
          </Field>

          <Field label="Response">
            <textarea value={form.response} onChange={e=>setForm(p=>({...p, response:e.target.value}))}
              placeholder="What the bot says. Use $user, $channel, $count"
              rows={3} style={{ ...C.input, lineHeight:1.5 }}
              onFocus={e=>e.target.style.borderColor="var(--cyan)"} onBlur={e=>e.target.style.borderColor="var(--border)"} />
          </Field>

          <div style={{ display:"flex", gap:12 }}>
            <Field label="Permission">
              <CyanSelect value={form.permission} onChange={e=>setForm(p=>({...p, permission:e.target.value}))}>
                {PERMS.map(p => <option key={p} value={p}>{p.charAt(0).toUpperCase()+p.slice(1)}</option>)}
              </CyanSelect>
            </Field>
            <Field label="Cooldown (seconds)">
              <CyanInput type="number" min={0} max={3600} value={form.cooldown_seconds}
                onChange={e=>setForm(p=>({...p, cooldown_seconds:e.target.value}))} style={{ width:120 }} />
            </Field>
          </div>

          <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", padding:"8px 12px", borderRadius:6, background:"rgba(8,11,15,0.6)", border:"1px solid var(--border)" }}>
            Variables: <span style={{ color:"var(--cyan)" }}>$user</span> (username) · <span style={{ color:"var(--cyan)" }}>$channel</span> (channel name) · <span style={{ color:"var(--cyan)" }}>$count</span> (times used)
          </div>

          {err && <div style={{ fontSize:12, color:"var(--red)", fontFamily:"'Outfit',sans-serif", padding:"8px 12px", borderRadius:6, background:"var(--red-dim)", border:"1px solid rgba(255,77,109,0.3)" }}>⚠️ {err}</div>}

          <div style={{ display:"flex", justifyContent:"flex-end", gap:10 }}>
            <button onClick={()=>setShowModal(false)} style={C.btnSecondary}>Cancel</button>
            <button onClick={saveCmd} disabled={saving||!form.command_name||!form.response} style={{ ...C.btnPrimary, opacity:saving||!form.command_name||!form.response?0.4:1 }}>{saving?"Saving...":editing?"Save Changes":"Add Command"}</button>
          </div>
        </Modal>
      )}
    </div>
  );
}


// ── Channel Rewards Tab ───────────────────────────────────────────────────────
function HotkeyRecorder({ value, onChange }) {
  const [recording, setRecording] = React.useState(false);
  const [keys, setKeys]           = React.useState(new Set());

  const startRecording = (e) => {
    e.preventDefault();
    setKeys(new Set());
    setRecording(true);
  };

  const stopRecording = () => {
    setRecording(false);
    setKeys(new Set());
  };

  React.useEffect(() => {
    if (!recording) return;

    const held = new Set();

    const onDown = (e) => {
      // Ignore mouse-triggered events
      if (e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents === false && e.isTrusted) {
        // keyboard event only
      }
      e.preventDefault();
      // Build key combo
      const parts = [];
      if (e.ctrlKey)  parts.push("ctrl");
      if (e.altKey)   parts.push("alt");
      if (e.shiftKey) parts.push("shift");
      if (e.metaKey)  parts.push("win");
      const key = e.key.toLowerCase();
      if (!["control","alt","shift","meta"].includes(key)) {
        parts.push(key === " " ? "space" : key);
      }
      if (parts.length > 0) {
        const combo = parts.join("+");
        held.add(combo);
        setKeys(new Set(held));
        onChange(combo);
      }
    };

    const onUp = () => {
      // Stop recording after first complete keypress
      setRecording(false);
    };

    window.addEventListener("keydown", onDown, true);
    window.addEventListener("keyup",   onUp,   true);
    return () => {
      window.removeEventListener("keydown", onDown, true);
      window.removeEventListener("keyup",   onUp,   true);
    };
  }, [recording, onChange]);

  const display = value || "Not set";
  const isRecording = recording;

  return (
    <div style={{ display:"flex", gap:8, alignItems:"center" }}>
      <div style={{
        flex:1, padding:"8px 12px", borderRadius:6,
        background:"rgba(8,11,15,0.8)", border:`1px solid ${isRecording ? "var(--cyan)" : "var(--border2)"}`,
        color: value ? "var(--cyan)" : "var(--text3)",
        fontFamily:"'JetBrains Mono',monospace", fontSize:12,
        boxShadow: isRecording ? "0 0 0 3px rgba(0,245,212,0.1)" : "none",
        transition:"all 0.15s", minWidth:120,
      }}>
        {isRecording ? "🔴 Press keys now…" : display}
      </div>
      {!isRecording ? (
        <button
          onMouseDown={startRecording}
          style={{ ...C.btnSecondary, padding:"6px 12px", fontSize:12, flexShrink:0 }}>
          {value ? "Re-record" : "Record"}
        </button>
      ) : (
        <button
          onClick={stopRecording}
          style={{ ...C.btnDanger, padding:"6px 12px", fontSize:12, flexShrink:0 }}>
          Cancel
        </button>
      )}
      {value && !isRecording && (
        <button
          onClick={() => onChange("")}
          style={{ ...C.btnDanger, padding:"6px 10px", fontSize:12, flexShrink:0 }}>
          ✕
        </button>
      )}
    </div>
  );
}

function ChannelRewardsTab({ guildId }) {
  const [bcast, setBcast]         = useState(null);
  const [loading, setLoading]     = useState(true);
  const [triggerModal, setTriggerModal] = useState(null);
  const [rewardModal, setRewardModal]   = useState(null);
  const [triggerForm, setTriggerForm]   = useState({ video_url:"", volume:1.0, hotkey:"" });
  const [rewardForm, setRewardForm]     = useState({ title:"", cost:100, is_enabled:true });
  const [saving, setSaving]       = useState(false);
  const [copied, setCopied]       = useState(false);
  const [overlayVolume, setOverlayVolume] = useState(100);
  const [volumeSaved, setVolumeSaved]     = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch(`/api/guild/${guildId}/broadcaster`);
      setBcast(data);
      setOverlayVolume(data.overlay_volume ?? 100);
    }
    catch(e) { console.error(e); }
    setLoading(false);
  }, [guildId]);

  useEffect(() => { load(); }, [load]);

  const connect = () => { window.location.href = `${window.location.origin}/auth/twitch/login/${guildId}`; };
  const disconnect = async () => {
    if (!confirm("Disconnect your Twitch account from this server?")) return;
    await apiFetch(`/api/guild/${guildId}/broadcaster`, { method:"DELETE" });
    load();
  };

  const openTrigger = (reward) => {
    const existing = bcast?.triggers?.find(t => t.reward_id === reward.id);
    setTriggerForm({ video_url: existing?.video_url || "", volume: existing?.volume ?? 1.0, hotkey: existing?.hotkey || "" });
    setTriggerModal(reward);
  };

  const saveTrigger = async () => {
    setSaving(true);
    try {
      await apiFetch(`/api/guild/${guildId}/broadcaster/triggers`, { method:"POST", body: JSON.stringify({
          reward_id:    triggerModal.id,
          reward_title: triggerModal.title,
          video_url:    triggerForm.video_url.trim(),
          volume:       Number(triggerForm.volume),
          hotkey:       triggerForm.hotkey.trim() || null,
        })});
      setTriggerModal(null); load();
    } catch(e) { alert("Failed: " + e.message); }
    setSaving(false);
  };

  const saveReward = async () => {
    setSaving(true);
    try {
      if (rewardModal === "add") {
        await apiFetch(`/api/guild/${guildId}/broadcaster/rewards`, { method:"POST", body: JSON.stringify(rewardForm) });
      } else {
        await apiFetch(`/api/guild/${guildId}/broadcaster/rewards/${rewardModal.id}`, { method:"PATCH", body: JSON.stringify(rewardForm) });
      }
      setRewardModal(null); load();
    } catch(e) {
      if (e.message?.includes("403") || e.message?.toLowerCase().includes("forbidden")) {
        alert("This reward was created directly on Twitch and cannot be edited here. Use your Twitch dashboard to edit it.");
      } else {
        alert("Failed: " + e.message);
      }
    }
    setSaving(false);
  };

  const deleteReward = async (rewardId) => {
    if (!confirm("Delete this reward from Twitch? This cannot be undone.")) return;
    try { await apiFetch(`/api/guild/${guildId}/broadcaster/rewards/${rewardId}`, { method:"DELETE" }); load(); }
    catch(e) {
      if (e.message?.includes("403") || e.message?.toLowerCase().includes("forbidden")) {
        alert("This reward was created directly on Twitch and cannot be deleted here. Use your Twitch dashboard to delete it.");
      } else {
        alert("Failed: " + e.message);
      }
    }
  };

  const copyOverlay = () => {
    navigator.clipboard.writeText(bcast?.overlay_url || "");
    setCopied(true); setTimeout(() => setCopied(false), 2000);
  };

  const saveVolume = async (val) => {
    setOverlayVolume(val);
    try {
      await apiFetch(`/api/guild/${guildId}/twitch/overlay-volume`, { method:"POST", body: JSON.stringify({ volume: val }) });
      setVolumeSaved(true);
      setTimeout(() => setVolumeSaved(false), 2000);
    } catch(e) { console.error("Failed to save volume:", e); }
  };

  const testOverlay = async () => {
    try {
      await apiFetch(`/api/guild/${guildId}/twitch/overlay-volume`, { method:"POST", body: JSON.stringify({ volume: overlayVolume }) });
      await apiFetch(`/api/guild/${guildId}/twitch/play-test`, { method:"POST" });
    } catch(e) { alert("Test failed — is OBS browser source open? " + e.message); }
  };

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  if (!bcast?.connected) return (
    <div>
      <PageHeader title="Channel Rewards" subtitle="Connect your Twitch account to manage channel point rewards" />
      <div style={{ ...C.card, textAlign:"center", padding:"48px 0" }}>
        <div style={{ fontSize:32, marginBottom:12 }}>🟣</div>
        <div style={{ fontWeight:700, fontSize:15, fontFamily:"'Orbitron',sans-serif", color:"var(--text)", marginBottom:8 }}>
          {bcast?.expired ? "Twitch connection expired" : "No Twitch account connected"}
        </div>
        <div style={{ fontSize:13, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", marginBottom:20 }}>
          Connect your Twitch broadcaster account to manage channel point rewards and set up video triggers.
        </div>
        <button onClick={connect} style={{ ...C.btnPrimary, padding:"10px 28px", fontSize:14 }}>🟣 Connect Twitch Account</button>
      </div>
    </div>
  );

  if (bcast?.not_affiliate) return (
    <div>
      <PageHeader title="Channel Rewards"
        subtitle={<span>Connected as <span style={{ color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>#{bcast.twitch_login}</span></span>}
        action={<button onClick={disconnect} style={{ ...C.btnDanger, fontSize:12 }}>Disconnect</button>}
      />
      <div style={{ ...C.card, textAlign:"center", padding:"48px 24px", marginBottom:16 }}>
        <div style={{ fontSize:32, marginBottom:12 }}>🎗️</div>
        <div style={{ fontWeight:700, fontSize:15, fontFamily:"'Orbitron',sans-serif", color:"var(--text)", marginBottom:8 }}>Affiliate required for rewards</div>
        <div style={{ fontSize:13, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", marginBottom:12 }}>
          Channel point rewards are only available to Twitch Affiliates and Partners.<br/>
          Keep grinding — you'll get there! 👊
        </div>
        <div style={{ fontSize:12, padding:"8px 16px", borderRadius:8, background:"rgba(0,245,212,0.06)", border:"1px solid rgba(0,245,212,0.2)", color:"var(--cyan)", display:"inline-block", fontFamily:"'Outfit',sans-serif" }}>
          ✓ Your account is connected — Twitch chat commands are available in the Chat Commands tab
        </div>
      </div>
      {bcast.overlay_url && (
        <div style={{ ...C.card }}>
          <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", marginBottom:4, textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>🎬 OBS Browser Source</div>
          <div style={{ fontSize:13, color:"var(--text3)", marginBottom:10, fontFamily:"'Outfit',sans-serif" }}>
            Add this URL as a browser source in OBS to use <code style={{ color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>!play</code> from Twitch chat.
          </div>
          <div style={{ display:"flex", gap:8, alignItems:"center", marginBottom:12 }}>
            <code style={{ flex:1, padding:"8px 12px", borderRadius:6, background:"rgba(8,11,15,0.8)", border:"1px solid var(--border)", color:"var(--cyan2)", fontSize:12, fontFamily:"'JetBrains Mono',monospace", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{bcast.overlay_url}</code>
            <button onClick={copyOverlay} style={{ ...C.btnPrimary, padding:"8px 16px", flexShrink:0 }}>{copied ? "✓ Copied" : "Copy"}</button>
          </div>
          <div style={{ display:"flex", alignItems:"center", gap:10 }}>
            <span style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", flexShrink:0 }}>🔊 Volume</span>
            <input type="range" min={0} max={100} value={overlayVolume}
              onChange={e => setOverlayVolume(Number(e.target.value))}
              onMouseUp={e => saveVolume(Number(e.target.value))}
              onTouchEnd={e => saveVolume(Number(e.target.value))}
              style={{ flex:1, accentColor:"var(--cyan)" }}
            />
            <span style={{ fontSize:12, fontFamily:"'JetBrains Mono',monospace", color:"var(--cyan)", minWidth:36, textAlign:"right" }}>{overlayVolume}%</span>
            <button onClick={testOverlay} style={{ ...C.btnSecondary, padding:"4px 10px", fontSize:12, flexShrink:0 }}>▶ Test</button>
            {volumeSaved && <span style={{ fontSize:11, color:"var(--green)", fontFamily:"'Outfit',sans-serif" }}>✓</span>}
          </div>
        </div>
      )}
    </div>
  );

  return (
    <div>
      <PageHeader title="Channel Rewards"
        subtitle={<span>Connected as <span style={{ color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>#{bcast.twitch_login}</span></span>}
        action={<button onClick={disconnect} style={{ ...C.btnDanger, fontSize:12 }}>Disconnect</button>}
      />

      {/* Overlay URL */}
      <div style={{ ...C.card, marginBottom:16 }}>
        <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", marginBottom:4, textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>🎬 OBS Browser Source</div>
        <div style={{ fontSize:13, color:"var(--text3)", marginBottom:10, fontFamily:"'Outfit',sans-serif" }}>Add this URL as a browser source in OBS. Keep it open during your stream.</div>
        <div style={{ display:"flex", gap:8, alignItems:"center", marginBottom:12 }}>
          <code style={{ flex:1, padding:"8px 12px", borderRadius:6, background:"rgba(8,11,15,0.8)", border:"1px solid var(--border)", color:"var(--cyan2)", fontSize:12, fontFamily:"'JetBrains Mono',monospace", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{bcast.overlay_url}</code>
          <button onClick={copyOverlay} style={{ ...C.btnPrimary, padding:"8px 16px", flexShrink:0 }}>{copied ? "✓ Copied" : "Copy"}</button>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:10 }}>
          <span style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", flexShrink:0 }}>🔊 Volume</span>
          <input type="range" min={0} max={100} value={overlayVolume}
            onChange={e => setOverlayVolume(Number(e.target.value))}
            onMouseUp={e => saveVolume(Number(e.target.value))}
            onTouchEnd={e => saveVolume(Number(e.target.value))}
            style={{ flex:1, accentColor:"var(--cyan)" }}
          />
          <span style={{ fontSize:12, fontFamily:"'JetBrains Mono',monospace", color:"var(--cyan)", minWidth:36, textAlign:"right" }}>{overlayVolume}%</span>
          <button onClick={testOverlay} style={{ ...C.btnSecondary, padding:"4px 10px", fontSize:12, flexShrink:0 }}>▶ Test</button>
          {volumeSaved && <span style={{ fontSize:11, color:"var(--green)", fontFamily:"'Outfit',sans-serif" }}>✓</span>}
        </div>
      </div>

      {/* Rewards list */}
      <div style={{ ...C.card }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:10 }}>
          <div style={{ fontWeight:800, fontSize:14, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>Channel Point Rewards</div>
          <button onClick={() => { setRewardForm({ title:"", cost:100, is_enabled:true }); setRewardModal("add"); }} style={C.btnPrimary}>+ New Reward</button>
        </div>
        <div style={{ marginBottom:14, padding:"8px 12px", borderRadius:6, border:"1px solid var(--border2)", background:"rgba(8,11,15,0.6)", color:"var(--text3)", fontSize:11, fontFamily:"'JetBrains Mono',monospace" }}>
          💡 Rewards created directly on Twitch can have video triggers added but cannot be edited or deleted here.
        </div>

        {bcast.rewards.length === 0 ? (
          <div style={{ textAlign:"center", padding:"28px 0", color:"var(--text3)" }}>
            <div style={{ fontSize:24, marginBottom:6 }}>🎁</div>
            <div style={{ fontSize:13, fontFamily:"'Outfit',sans-serif" }}>No channel point rewards yet</div>
          </div>
        ) : (
          <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
            {bcast.rewards.map(reward => {
              const trigger = bcast.triggers?.find(t => t.reward_id === reward.id);
              return (
                <div key={reward.id} style={{ display:"flex", alignItems:"center", gap:12, padding:"12px 14px", borderRadius:8, background:"rgba(8,11,15,0.6)", border:"1px solid var(--border)" }}>
                  <div style={{ width:14, height:14, borderRadius:"50%", background:reward.background_color, flexShrink:0, boxShadow:`0 0 6px ${reward.background_color}88` }} />
                  <div style={{ flex:1, minWidth:0 }}>
                    <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:2 }}>
                      <span style={{ fontWeight:600, fontSize:13, color:reward.is_enabled?"var(--text)":"var(--text3)", fontFamily:"'Outfit',sans-serif" }}>{reward.title}</span>
                      <span style={{ fontSize:11, color:"var(--yellow)", fontFamily:"'JetBrains Mono',monospace" }}>{reward.cost.toLocaleString()} pts</span>
                      {!reward.is_enabled && <Badge text="disabled" color="var(--text3)" />}
                    </div>
                    <div style={{ display:"flex", gap:8, flexWrap:"wrap", marginTop:2 }}>
                      {trigger?.video_url ? (
                        <span style={{ fontSize:11, color:"var(--green)", fontFamily:"'JetBrains Mono',monospace" }}>🎬 Video</span>
                      ) : null}
                      {trigger?.hotkey ? (
                        <span style={{ fontSize:11, color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace", background:"rgba(0,245,212,0.08)", padding:"1px 6px", borderRadius:4, border:"1px solid rgba(0,245,212,0.2)" }}>⌨ {trigger.hotkey}</span>
                      ) : null}
                      {!trigger?.video_url && !trigger?.hotkey ? (
                        <span style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>No triggers set</span>
                      ) : null}
                    </div>
                  </div>
                  <button onClick={() => openTrigger(reward)} style={{ ...C.btnSecondary, padding:"5px 12px", fontSize:12, flexShrink:0 }}>{trigger ? "Edit Trigger" : "+ Trigger"}</button>
                  {reward.manageable && <button onClick={() => { setRewardForm({ title:reward.title, cost:reward.cost, is_enabled:reward.is_enabled }); setRewardModal(reward); }} style={{ ...C.btnSecondary, padding:"5px 12px", fontSize:12, flexShrink:0 }}>Edit</button>}
                  {reward.manageable && <button onClick={() => deleteReward(reward.id)} style={{ ...C.btnDanger, flexShrink:0 }}>Delete</button>}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Trigger modal */}
      {triggerModal && (
        <Modal onClose={() => setTriggerModal(null)} width={460}>
          <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>
            🎬 Video Trigger — {triggerModal.title}
          </div>
          <Field label="Video URL">
            <CyanInput value={triggerForm.video_url} onChange={e=>setTriggerForm(p=>({...p,video_url:e.target.value}))}
              placeholder="https://www.youtube.com/watch?v=..." />
            <div style={{ fontSize:11, color:"var(--text3)", marginTop:3, fontFamily:"'JetBrains Mono',monospace" }}>Leave blank to remove trigger.</div>
          </Field>
          <Field label="Volume (0.0 – 1.0)">
            <div style={{ display:"flex", alignItems:"center", gap:10 }}>
              <input type="range" min={0} max={1} step={0.05} value={triggerForm.volume}
                onChange={e=>setTriggerForm(p=>({...p,volume:parseFloat(e.target.value)}))}
                style={{ flex:1, accentColor:"var(--cyan)" }} />
              <span style={{ fontSize:12, color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace", width:32 }}>{Number(triggerForm.volume).toFixed(2)}</span>
            </div>
          </Field>
          <Field label="Companion Hotkey">
            <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:6 }}>
              Fires on your PC when this reward is redeemed (stream must be live, Companion must be running).
            </div>
            <HotkeyRecorder
              value={triggerForm.hotkey}
              onChange={v => setTriggerForm(p=>({...p, hotkey:v}))}
            />
          </Field>
          <div style={{ display:"flex", justifyContent:"flex-end", gap:10 }}>
            <button onClick={() => setTriggerModal(null)} style={C.btnSecondary}>Cancel</button>
            <button onClick={saveTrigger} disabled={saving} style={{ ...C.btnPrimary, opacity:saving?0.4:1 }}>{saving?"Saving...":"Save"}</button>
          </div>
        </Modal>
      )}

      {/* Reward add/edit modal */}
      {rewardModal && (
        <Modal onClose={() => setRewardModal(null)} width={420}>
          <div style={{ fontWeight:800, fontSize:16, fontFamily:"'Orbitron',sans-serif", color:"var(--cyan)", textShadow:"0 0 10px rgba(0,245,212,0.4)" }}>
            {rewardModal === "add" ? "New Channel Reward" : `Edit — ${rewardModal.title}`}
          </div>
          <Field label="Title"><CyanInput value={rewardForm.title} onChange={e=>setRewardForm(p=>({...p,title:e.target.value}))} placeholder="Reward name" /></Field>
          <Field label="Cost (points)"><CyanInput type="number" min={1} value={rewardForm.cost} onChange={e=>setRewardForm(p=>({...p,cost:parseInt(e.target.value)||100}))} /></Field>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"10px 0", borderTop:"1px solid var(--border)" }}>
            <span style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>Enabled</span>
            <button onClick={() => setRewardForm(p=>({...p,is_enabled:!p.is_enabled}))}
              style={{ width:44, height:24, borderRadius:12, border:"none", cursor:"pointer", background:rewardForm.is_enabled?"var(--cyan)":"var(--border2)", position:"relative", transition:"background 0.2s", boxShadow:rewardForm.is_enabled?"0 0 10px rgba(0,245,212,0.4)":"none" }}>
              <div style={{ position:"absolute", top:3, left:rewardForm.is_enabled?22:3, width:18, height:18, borderRadius:"50%", background:"#fff", transition:"left 0.2s" }} />
            </button>
          </div>
          <div style={{ display:"flex", justifyContent:"flex-end", gap:10 }}>
            <button onClick={() => setRewardModal(null)} style={C.btnSecondary}>Cancel</button>
            <button onClick={saveReward} disabled={saving||!rewardForm.title} style={{ ...C.btnPrimary, opacity:saving||!rewardForm.title?0.4:1 }}>{saving?"Saving...":rewardModal==="add"?"Create Reward":"Save Changes"}</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

// ── Dev: Global Stats Tab ─────────────────────────────────────────────────────
function GlobalStatsTab() {
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lbSort, setLbSort] = useState("consistency");  // 'consistency' | 'hours' | 'longest'
  const load = useCallback(async () => {
    setLoading(true);
    try { setStats(await apiFetch("/api/dev/global-stats")); }
    catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  const StatCard = ({ title, value, sub, color="var(--cyan)" }) => (
    <div style={{ ...C.card, textAlign:"center", padding:"20px 14px" }}>
      <div style={{ fontSize:28, fontWeight:800, fontFamily:"'Orbitron',sans-serif", color }}>{value?.toLocaleString?.() ?? value}</div>
      <div style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif", marginTop:4 }}>{title}</div>
      {sub && <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>{sub}</div>}
    </div>
  );

  return (
    <div>
      <PageHeader title="Global Stats" subtitle="Live overview across all servers" />
      <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill,minmax(160px,1fr))", gap:12, marginBottom:20 }}>
        <StatCard title="Servers" value={stats?.total_servers} />
        <StatCard title="Streamer Rows" value={stats?.total_streamer_rows} />
        <StatCard title="Unique Streamers" value={stats?.unique_streamers} />
        <StatCard title="Active Notif IDs" value={stats?.active_notifications} />
        <StatCard title="Notifs (24h)" value={stats?.notifications_24h} color="var(--green)" />
        <StatCard title="Currently Live" value={stats?.live_count} color="var(--green)" />
        <StatCard title="EventSub Subs" value={stats?.eventsub_count} color="var(--yellow)" />
      </div>

      {stats?.live_streamers?.length > 0 && (
        <div style={{ ...C.card, marginBottom:16 }}>
          <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:15, color:"var(--green)", textShadow:"0 0 14px rgba(57,217,138,0.4), 0 0 28px rgba(57,217,138,0.15)", marginBottom:12 }}>🟢 Currently Live ({stats.live_count})</div>
          <div style={{ display:"flex", flexWrap:"wrap", gap:6 }}>
            {stats.live_streamers.map(s => (
              <a key={s} href={`https://twitch.tv/${s}`} target="_blank" rel="noreferrer"
                style={{ padding:"3px 10px", borderRadius:20, background:"rgba(57,217,138,0.1)", border:"1px solid rgba(57,217,138,0.3)", color:"var(--green)", fontSize:12, fontFamily:"'JetBrains Mono',monospace", textDecoration:"none" }}>
                {s}
              </a>
            ))}
          </div>
        </div>
      )}

      <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16, marginBottom:16 }}>
        <div style={{ ...C.card }}>
          <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:15, color:"var(--text)", textShadow:"0 0 14px rgba(0,245,212,0.3), 0 0 28px rgba(0,245,212,0.1)", marginBottom:12 }}>🔥 Top Tracked Streamers</div>
          <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
            {stats?.top_streamers?.map((s,i) => (
              <div key={s.streamer_name} style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"6px 0", borderBottom:"1px solid var(--border)" }}>
                <span style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>
                  <span style={{ color:"var(--text3)", fontSize:11, marginRight:8 }}>#{i+1}</span>{s.streamer_name}
                </span>
                <span style={{ fontSize:11, color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>{s.server_count} server{s.server_count !== 1 ? "s" : ""}</span>
              </div>
            ))}
          </div>
        </div>
        <div style={{ ...C.card }}>
          <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:15, color:"var(--text)", textShadow:"0 0 14px rgba(0,245,212,0.3), 0 0 28px rgba(0,245,212,0.1)", marginBottom:12 }}>🏆 Servers by Streamer Count</div>
          <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
            {stats?.servers_by_count?.map((s,i) => (
              <div key={s.guild_id} style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"6px 0", borderBottom:"1px solid var(--border)" }}>
                <span style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", maxWidth:"70%" }}>
                  <span style={{ color:"var(--text3)", fontSize:11, marginRight:8 }}>#{i+1}</span>{s.name}
                </span>
                <span style={{ fontSize:11, color:"var(--cyan)", fontFamily:"'JetBrains Mono',monospace" }}>{s.streamer_count}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {(() => {
        // Pick the active leaderboard array based on the selected sort tab.
        // Backwards compat: fall back to legacy `global_leaderboard` field if
        // the new per-sort fields aren't present (e.g. stale API).
        const lbData = {
          consistency: stats?.global_leaderboard_consistency ?? stats?.global_leaderboard ?? [],
          hours:       stats?.global_leaderboard_hours       ?? [],
          longest:     stats?.global_leaderboard_longest     ?? [],
        };
        const lbMeta = {
          consistency: { emoji: "🏆", label: "Most Active",         desc: "Most active streamers across all servers — month to date" },
          hours:       { emoji: "⏱️", label: "Most Hours Streamed", desc: "Streamers with the most total hours this month" },
          longest:     { emoji: "💀", label: "Longest Stream",      desc: "Streamers with the longest single sessions this month" },
        };
        const activeRows = lbData[lbSort] || [];
        const activeMeta = lbMeta[lbSort];

        // Only show the card if ANY sort has data — avoids an empty card on fresh DBs
        const anyData = lbData.consistency.length > 0 || lbData.hours.length > 0 || lbData.longest.length > 0;
        if (!anyData) return null;

        const TabBtn = ({ id }) => {
          const m = lbMeta[id];
          const active = lbSort === id;
          return (
            <button
              onClick={() => setLbSort(id)}
              style={{
                background: active ? "rgba(0,245,212,0.12)" : "transparent",
                border: `1px solid ${active ? "rgba(0,245,212,0.5)" : "var(--border)"}`,
                color: active ? "var(--cyan)" : "var(--text3)",
                padding: "5px 10px",
                borderRadius: 6,
                cursor: "pointer",
                fontSize: 11,
                fontFamily: "'JetBrains Mono',monospace",
                whiteSpace: "nowrap",
              }}>
              {m.emoji} {m.label}
            </button>
          );
        };

        return (
          <div style={{ ...C.card, marginBottom:16 }}>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:4, gap:10, flexWrap:"wrap" }}>
              <div style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:15, color:"var(--text)", textShadow:"0 0 14px rgba(0,245,212,0.3), 0 0 28px rgba(0,245,212,0.1)" }}>
                🌍 {activeMeta.label}
              </div>
              <div style={{ display:"flex", gap:6, flexWrap:"wrap" }}>
                <TabBtn id="consistency" />
                <TabBtn id="hours" />
                <TabBtn id="longest" />
              </div>
            </div>
            <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:12 }}>{activeMeta.desc}</div>
            {activeRows.length === 0 ? (
              <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'Outfit',sans-serif", padding:"12px 0" }}>
                No streamers with completed sessions yet this month.
              </div>
            ) : (
              <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
                {activeRows.map((s,i) => {
                  const medal = ["🥇","🥈","🥉"][i] || `#${i+1}`;
                  return (
                    <div key={s.streamer_name} style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"6px 0", borderBottom:"1px solid var(--border)", gap:10 }}>
                      <div style={{ display:"flex", alignItems:"center", gap:8, minWidth:0, flex:"0 1 auto" }}>
                        <span style={{ color:"var(--text3)", fontSize:11, fontFamily:"'JetBrains Mono',monospace", minWidth:24 }}>{medal}</span>
                        <a href={`https://twitch.tv/${s.streamer_name}`} target="_blank" rel="noreferrer"
                           style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif", textDecoration:"none", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>
                          {s.streamer_name}
                        </a>
                      </div>
                      <div style={{ display:"flex", gap:6, flexWrap:"wrap", justifyContent:"flex-end", fontSize:11, fontFamily:"'JetBrains Mono',monospace" }}>
                        <span style={{ color:"var(--cyan)" }}>{s.total_streams} stream{s.total_streams !== 1 ? "s" : ""}</span>
                        <span style={{ color:"var(--text3)" }}>·</span>
                        <span style={{ color:"var(--cyan)" }}>{s.server_count} server{s.server_count !== 1 ? "s" : ""}</span>
                        {s.hours_streamed > 0 && (
                          <>
                            <span style={{ color:"var(--text3)" }}>·</span>
                            <span style={{ color:"var(--green)" }}>{s.hours_streamed}h</span>
                          </>
                        )}
                        {s.longest_hours > 0 && (
                          <>
                            <span style={{ color:"var(--text3)" }}>·</span>
                            <span style={{ color:"var(--yellow)" }}>{s.longest_hours}h longest</span>
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })()}

      <div style={{ display:"flex", justifyContent:"flex-end" }}>
        <button onClick={load} style={{ ...C.btnSecondary, fontSize:12 }}>↻ Refresh</button>
      </div>
    </div>
  );
}

// ── Dev: Stream Events Viewer (sub-component of DB Tools) ─────────────────────
function StreamEventsViewer() {
  const [streamer, setStreamer] = useState("");
  const [month, setMonth] = useState(""); // empty = current month
  const [limit, setLimit] = useState(100);
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Build month options: current and previous
  const monthOptions = (() => {
    const now = new Date();
    const cur = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    const prevDate = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    const prev = `${prevDate.getFullYear()}-${String(prevDate.getMonth() + 1).padStart(2, "0")}`;
    return [
      { value: "", label: `Current (${cur})` },
      { value: prev, label: `Previous (${prev})` },
    ];
  })();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (streamer.trim()) params.set("streamer", streamer.trim());
      if (month) params.set("month", month);
      params.set("limit", String(limit));
      const data = await apiFetch(`/api/dev/stream-events?${params}`);
      setEvents(data.events || []);
      if (data.error) setError(data.error);
    } catch (e) {
      setError(e.message || String(e));
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, [streamer, month, limit]);

  useEffect(() => { load(); }, []);  // initial load only; manual reload via button

  const formatLocal = (utcStr) => {
    if (!utcStr) return "—";
    // SQLite naive UTC timestamp: 'YYYY-MM-DD HH:MM:SS'. Make it explicit UTC.
    const isoUtc = utcStr.replace(" ", "T") + "Z";
    try {
      const d = new Date(isoUtc);
      if (Number.isNaN(d.getTime())) return utcStr;
      return d.toLocaleString(undefined, {
        year: "numeric", month: "short", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      });
    } catch {
      return utcStr;
    }
  };

  const statusColor = (s) => {
    if (s === "live") return "var(--green)";
    if (s === "orphan") return "var(--yellow)";
    return "var(--text3)";
  };

  return (
    <div style={{ ...C.card, padding:"14px 18px" }}>
      <div style={{ display:"flex", gap:8, flexWrap:"wrap", alignItems:"center", marginBottom:12 }}>
        <CyanInput
          type="text"
          placeholder="Streamer name (substring)"
          value={streamer}
          onChange={(e) => setStreamer(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") load(); }}
          style={{ flex:"1 1 200px", minWidth:160 }}
        />
        <select
          value={month}
          onChange={(e) => setMonth(e.target.value)}
          style={{ background:"var(--bg2)", color:"var(--text)", border:"1px solid var(--border)", borderRadius:6, padding:"6px 8px", fontSize:12, fontFamily:"'JetBrains Mono',monospace" }}
        >
          {monthOptions.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select
          value={limit}
          onChange={(e) => setLimit(parseInt(e.target.value, 10) || 100)}
          style={{ background:"var(--bg2)", color:"var(--text)", border:"1px solid var(--border)", borderRadius:6, padding:"6px 8px", fontSize:12, fontFamily:"'JetBrains Mono',monospace" }}
        >
          <option value={50}>50 rows</option>
          <option value={100}>100 rows</option>
          <option value={200}>200 rows</option>
          <option value={500}>500 rows</option>
        </select>
        <button onClick={load} disabled={loading} style={{ ...C.btnSecondary, fontSize:12, opacity: loading ? 0.5 : 1 }}>
          {loading ? "Loading…" : "Search"}
        </button>
      </div>

      {error && (
        <div style={{ fontSize:12, color:"var(--red)", fontFamily:"'JetBrains Mono',monospace", marginBottom:8 }}>
          Error: {error}
        </div>
      )}

      <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:6 }}>
        {events.length === 0 && !loading ? "No events found." : `${events.length} event${events.length === 1 ? "" : "s"}`}
      </div>

      {events.length > 0 && (
        <div style={{ borderTop:"1px solid var(--border)", maxHeight:440, overflowY:"auto" }}>
          <div style={{ display:"grid", gridTemplateColumns:"1.4fr 1.5fr 1.5fr 0.6fr 0.5fr", gap:14, fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", padding:"8px 32px 8px 4px", borderBottom:"1px solid var(--border)", textTransform:"uppercase", alignItems:"center" }}>
            <div>Streamer</div>
            <div>Went Live</div>
            <div>Ended At</div>
            <div style={{ textAlign:"right" }}>Hours</div>
            <div style={{ textAlign:"right" }}>Status</div>
          </div>
          {events.map(ev => (
            <div key={ev.id} style={{ display:"grid", gridTemplateColumns:"1.4fr 1.5fr 1.5fr 0.6fr 0.5fr", gap:14, fontSize:11, padding:"6px 32px 6px 4px", borderBottom:"1px solid var(--border)", alignItems:"center", fontFamily:"'JetBrains Mono',monospace" }}>
              <a href={`https://twitch.tv/${ev.streamer_name}`} target="_blank" rel="noreferrer" style={{ color:"var(--text)", textDecoration:"none", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", minWidth:0 }}>
                {ev.streamer_name}
              </a>
              <div style={{ color:"var(--text2)", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", minWidth:0 }}>{formatLocal(ev.went_live_at)}</div>
              <div style={{ color: ev.ended_at ? "var(--text2)" : "var(--text3)", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", minWidth:0 }}>{formatLocal(ev.ended_at)}</div>
              <div style={{ textAlign:"right", color: ev.hours === null ? "var(--text3)" : (ev.hours > 12 ? "var(--yellow)" : "var(--cyan)") }}>
                {ev.hours === null ? "—" : `${ev.hours}h`}
              </div>
              <div style={{ textAlign:"right", color: statusColor(ev.status), textTransform:"uppercase", fontSize:10 }}>
                {ev.status}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Set Up Server Wizard ──────────────────────────────────────────────────────
function SetupWizardTab({ guildId, isDev }) {
  const TEMPLATES = [
    { id: "aesthetic",  label: "Aesthetic",  desc: "Curated channels with emoji prefixes for a polished, vibe-y feel." },
    { id: "simplistic", label: "Simplistic", desc: "Bare minimum: welcome, rules, general, live notifications, and one voice channel." },
    { id: "cluttered",  label: "Cluttered",  desc: "Everything-and-the-kitchen-sink: lots of channels organized by topic." },
  ];
  const ROLE_FIELDS = [
    { key: "member",    label: "Member role" },
    { key: "vip",       label: "VIP role" },
    { key: "moderator", label: "Moderator role" },
    { key: "admin",     label: "Admin role" },
    { key: "bot",       label: "Bot role" },
  ];

  const [step, setStep] = useState(0);
  const [config, setConfig] = useState({
    template: "simplistic",
    enable_verification: false,
    enable_vip: false,
    auto_post_rules: true,
    role_names: {},
    color: 0x00F5D4,
  });
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState(null);
  const [setupId, setSetupId] = useState(null);
  const [status, setStatus] = useState(null);
  const [applyError, setApplyError] = useState(null);
  const [confirmEstablished, setConfirmEstablished] = useState(false);

  // Poll status when we have a setup_id in flight
  useEffect(() => {
    if (!setupId) return;
    let active = true;
    const poll = async () => {
      try {
        const data = await apiFetch(`/api/guild/${guildId}/setup/status/${setupId}`);
        if (!active) return;
        setStatus(data);
        if (data.status === "done" || data.status === "error") return;
        setTimeout(poll, 500);
      } catch (e) {
        if (!active) return;
        setApplyError(e.message || String(e));
      }
    };
    poll();
    return () => { active = false; };
  }, [setupId, guildId]);

  const fetchPreview = useCallback(async () => {
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const data = await apiFetch(`/api/guild/${guildId}/setup/preview`, {
        method: "POST",
        body: JSON.stringify(config),
      });
      setPreview(data);
    } catch (e) {
      setPreviewError(e.message || String(e));
    } finally {
      setPreviewLoading(false);
    }
  }, [guildId, config]);

  const startApply = async (dry) => {
    setApplyError(null);
    setStatus(null);
    setSetupId(null);
    try {
      const data = await apiFetch(`/api/guild/${guildId}/setup/apply`, {
        method: "POST",
        body: JSON.stringify({
          config,
          dry_run: dry,
          confirm_established: confirmEstablished,
        }),
      });
      setSetupId(data.setup_id);
    } catch (e) {
      setApplyError(e.message || String(e));
    }
  };

  // Auto-fetch preview when we hit the review step. Also fetch eagerly on
  // initial mount and whenever the template changes, so the Template step
  // can show the established-server confirmation card and gate the Next
  // button BEFORE the user proceeds.
  useEffect(() => {
    fetchPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.template]);

  // Make sure the review step is up-to-date when entering it
  useEffect(() => {
    if (step === 3) fetchPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  // Helpers
  const stepDot = (i) => (
    <div style={{
      width:8, height:8, borderRadius:"50%",
      background: step === i ? "var(--cyan)" : (step > i ? "var(--green)" : "var(--text3)"),
      transition: "background 0.2s",
    }} />
  );

  const NavButtons = ({ canNext = true, nextLabel = "Next →" }) => (
    <div style={{ display:"flex", justifyContent:"space-between", marginTop:20 }}>
      <button onClick={() => setStep(s => Math.max(0, s - 1))} disabled={step === 0}
        style={{ ...C.btnSecondary, fontSize:13, opacity: step === 0 ? 0.4 : 1 }}>
        ← Back
      </button>
      <button onClick={() => setStep(s => s + 1)} disabled={!canNext}
        style={{ ...C.btnPrimary, fontSize:13, opacity: canNext ? 1 : 0.4 }}>
        {nextLabel}
      </button>
    </div>
  );

  return (
    <div>
      <PageHeader title="Set Up Server" subtitle="Walk through a guided template setup for this server" />

      {/* Step indicator */}
      <div style={{ display:"flex", alignItems:"center", gap:12, marginBottom:24 }}>
        {["Template", "Roles", "Modules", "Review", "Apply"].map((lbl, i) => (
          <div key={lbl} style={{ display:"flex", alignItems:"center", gap:12 }}>
            {stepDot(i)}
            <div style={{ fontSize:12, color: step === i ? "var(--cyan)" : "var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{lbl}</div>
            {i < 4 && <div style={{ flex:"0 0 24px", height:1, background:"var(--border)" }} />}
          </div>
        ))}
      </div>

      <div style={C.card}>
        {step === 0 && (
          <div>
            <div style={{ fontFamily:"'Orbitron',sans-serif", fontSize:16, fontWeight:800, marginBottom:6 }}>Pick a template</div>
            <div style={{ fontSize:12, color:"var(--text3)", marginBottom:18 }}>Each one creates a different number of channels. You can change toggles in the next steps.</div>
            <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
              {TEMPLATES.map(t => (
                <button key={t.id}
                  onClick={() => setConfig(c => ({ ...c, template: t.id }))}
                  style={{
                    background: config.template === t.id ? "rgba(0,245,212,0.08)" : "var(--bg2)",
                    border: `1px solid ${config.template === t.id ? "var(--cyan)" : "var(--border)"}`,
                    borderRadius:8,
                    padding:"14px 16px",
                    cursor:"pointer",
                    textAlign:"left",
                    color:"var(--text)",
                  }}>
                  <div style={{ fontWeight:700, fontSize:14, fontFamily:"'Outfit',sans-serif", marginBottom:4 }}>
                    {config.template === t.id && "✓ "}{t.label}
                  </div>
                  <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{t.desc}</div>
                </button>
              ))}
            </div>

            {/* Established-server confirmation (only shown if preview says
                the guild has existing content). Gates the Next button. */}
            {preview?.is_established && (
              <div style={{
                background:"rgba(255,107,53,0.08)",
                border:"1px solid var(--yellow)",
                borderRadius:8,
                padding:14,
                marginTop:14,
              }}>
                <div style={{ color:"var(--yellow)", fontWeight:700, fontSize:13, marginBottom:8 }}>
                  ⚠️ This server has existing content
                </div>
                <div style={{ fontSize:12, color:"var(--text2)", fontFamily:"'JetBrains Mono',monospace", lineHeight:1.6, marginBottom:10 }}>
                  Detected: {preview.establishment_signals?.channels} channels,
                  {" "}{preview.establishment_signals?.custom_roles} custom roles,
                  {" "}{preview.establishment_signals?.members} members.
                  <br />
                  The wizard will NEVER delete existing content, but it WILL:
                  <br />
                  • Create new roles/channels alongside existing ones
                  <br />
                  • Lock welcome and rules channels read-only (members can read but not write/react)
                  <br />
                  • Modify @everyone view permissions on community channels if verification is on
                  <br />
                  • Post a rules message in the rules channel
                  <br /><br />
                  Confirm below to continue with the wizard.
                </div>
                <label style={{ display:"flex", alignItems:"center", gap:10, cursor:"pointer", fontSize:13, color:"var(--text)" }}>
                  <input type="checkbox"
                    checked={confirmEstablished}
                    onChange={(e) => setConfirmEstablished(e.target.checked)}
                    style={{ width:18, height:18, cursor:"pointer" }} />
                  I understand and want to apply a template to this established server
                </label>
              </div>
            )}

            <NavButtons canNext={!preview?.is_established || confirmEstablished} />
          </div>
        )}

        {step === 1 && (
          <div>
            <div style={{ fontFamily:"'Orbitron',sans-serif", fontSize:16, fontWeight:800, marginBottom:6 }}>Role names</div>
            <div style={{ fontSize:12, color:"var(--text3)", marginBottom:18 }}>Leave blank to use the default ALL-CAPS names. These roles will be created if they don't already exist.</div>
            <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
              {ROLE_FIELDS.map(f => (
                <div key={f.key} style={{ display:"flex", alignItems:"center", gap:12 }}>
                  <div style={{ width:120, fontSize:13, color:"var(--text2)", fontFamily:"'Outfit',sans-serif" }}>{f.label}</div>
                  <CyanInput
                    type="text"
                    placeholder={f.key.toUpperCase()}
                    value={config.role_names[f.key] || ""}
                    onChange={(e) => setConfig(c => ({ ...c, role_names: { ...c.role_names, [f.key]: e.target.value } }))}
                    style={{ flex:1 }}
                  />
                </div>
              ))}
            </div>
            <NavButtons />
          </div>
        )}

        {step === 2 && (
          <div>
            <div style={{ fontFamily:"'Orbitron',sans-serif", fontSize:16, fontWeight:800, marginBottom:6 }}>Modules & options</div>
            <div style={{ fontSize:12, color:"var(--text3)", marginBottom:18 }}>Pick which optional features to enable.</div>
            <div style={{ display:"flex", flexDirection:"column", gap:14 }}>
              <ToggleRow
                label="Enable Verification"
                desc="Hide all channels except welcome and rules from new joiners. They click a Verify button to gain access."
                value={config.enable_verification}
                onChange={(v) => setConfig(c => ({ ...c, enable_verification: v }))}
              />
              <ToggleRow
                label="VIP module"
                desc="Adds a VIP category with vip-chat and VIP VC, locked to VIP role and staff."
                value={config.enable_vip}
                onChange={(v) => setConfig(c => ({ ...c, enable_vip: v }))}
              />
              <ToggleRow
                label="Auto-post rules"
                desc="Bot posts a generic rules message in the rules channel."
                value={config.auto_post_rules}
                onChange={(v) => setConfig(c => ({ ...c, auto_post_rules: v }))}
              />
              <div style={{ display:"flex", alignItems:"center", gap:12 }}>
                <div style={{ width:140, fontSize:13, color:"var(--text2)", fontFamily:"'Outfit',sans-serif" }}>Theme color</div>
                <input type="color"
                  value={"#" + (config.color || 0).toString(16).padStart(6, "0")}
                  onChange={(e) => setConfig(c => ({ ...c, color: parseInt(e.target.value.replace("#", ""), 16) }))}
                  style={{ width:60, height:32, border:"1px solid var(--border)", borderRadius:6, background:"var(--bg2)", cursor:"pointer" }} />
                <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>Applied to bot embed messages</div>
              </div>
            </div>
            <NavButtons />
          </div>
        )}

        {step === 3 && (
          <div>
            <div style={{ fontFamily:"'Orbitron',sans-serif", fontSize:16, fontWeight:800, marginBottom:6 }}>Review</div>
            <div style={{ fontSize:12, color:"var(--text3)", marginBottom:18 }}>Here's what will be created. Existing roles/channels with matching names will be reused.</div>

            {previewLoading && <div style={{ color:"var(--cyan)" }}>Loading preview…</div>}
            {previewError && <div style={{ color:"var(--red)", marginBottom:12 }}>Preview error: {previewError}</div>}

            {preview && (
              <div style={{ display:"flex", flexDirection:"column", gap:12 }}>
                {preview.missing_permissions?.length > 0 && (
                  <div style={{ background:"rgba(255,107,53,0.08)", border:"1px solid var(--yellow)", borderRadius:8, padding:12 }}>
                    <div style={{ color:"var(--yellow)", fontWeight:700, fontSize:13, marginBottom:6 }}>⚠️ Missing bot permissions</div>
                    <div style={{ fontSize:12, fontFamily:"'JetBrains Mono',monospace", color:"var(--text2)" }}>
                      The bot needs: {preview.missing_permissions.join(", ")}. Grant these in Server Settings → Roles before applying.
                    </div>
                  </div>
                )}

                <div style={{ display:"grid", gridTemplateColumns:"repeat(4, 1fr)", gap:10 }}>
                  <Stat label="Roles" value={preview.counts?.roles} />
                  <Stat label="Categories" value={preview.counts?.categories} />
                  <Stat label="Text channels" value={preview.counts?.text_channels} />
                  <Stat label="Voice channels" value={preview.counts?.voice_channels} />
                </div>

                {preview.would_reuse?.roles?.length > 0 && (
                  <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>
                    Will reuse existing roles: {preview.would_reuse.roles.join(", ")}
                  </div>
                )}
                {preview.would_reuse?.categories?.length > 0 && (
                  <div style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>
                    Will reuse existing categories: {preview.would_reuse.categories.join(", ")}
                  </div>
                )}

                {preview.role_permissions && (
                  <div style={{ borderTop:"1px solid var(--border)", paddingTop:10 }}>
                    <div style={{ fontSize:13, fontWeight:600, marginBottom:6 }}>Role permissions</div>
                    <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:8 }}>
                      Default permissions applied to NEWLY created roles. Existing reused roles keep their current permissions.
                    </div>
                    {Object.entries(preview.role_permissions).map(([key, flags]) => {
                      const name = preview.role_names?.[key] || key.toUpperCase();
                      const hasAdmin = flags.includes("administrator");
                      return (
                        <div key={key} style={{ marginBottom:8, fontSize:11, fontFamily:"'JetBrains Mono',monospace" }}>
                          <div style={{ color: hasAdmin ? "var(--yellow)" : "var(--cyan)", marginBottom:2 }}>
                            {name}{hasAdmin ? " ⚠️" : ""} <span style={{ color:"var(--text3)" }}>({flags.length} flag{flags.length === 1 ? "" : "s"})</span>
                          </div>
                          <div style={{ color:"var(--text3)", marginLeft:12, lineHeight:1.5 }}>
                            {flags.length === 0 ? "none" : flags.join(", ")}
                          </div>
                        </div>
                      );
                    })}
                    <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:6, padding:"8px 10px", background:"rgba(0,245,212,0.04)", border:"1px solid var(--border)", borderRadius:6 }}>
                      Admin role is created with every management permission EXCEPT the <b>Administrator</b> flag (the bot itself doesn't have it, so it can't grant it). After the wizard runs, you can manually toggle Administrator on in Server Settings → Roles if you want full unrestricted access.
                    </div>
                  </div>
                )}

                <div style={{ borderTop:"1px solid var(--border)", paddingTop:10 }}>
                  <div style={{ fontSize:13, fontWeight:600, marginBottom:8 }}>Structure</div>
                  {preview.categories?.map(cat => (
                    <div key={cat.name} style={{ marginBottom:8, fontSize:12, fontFamily:"'JetBrains Mono',monospace" }}>
                      <div style={{ color:"var(--cyan)", marginBottom:2 }}>{cat.name}</div>
                      {cat.channels.map(([name, type]) => (
                        <div key={name} style={{ color:"var(--text3)", marginLeft:12 }}>
                          {type === "voice" ? "🔊" : "#"} {name}
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            )}

            <NavButtons canNext={!!preview && !previewError} nextLabel="Apply →" />
          </div>
        )}

        {step === 4 && (
          <div>
            <div style={{ fontFamily:"'Orbitron',sans-serif", fontSize:16, fontWeight:800, marginBottom:6 }}>Apply</div>
            <div style={{ fontSize:12, color:"var(--text3)", marginBottom:18 }}>Run a dry-run first to validate, then apply for real.</div>

            {preview?.is_established && (
              <div style={{
                background:"rgba(255,107,53,0.08)",
                border:"1px solid var(--yellow)",
                borderRadius:8,
                padding:14,
                marginBottom:16,
              }}>
                <div style={{ color:"var(--yellow)", fontWeight:700, fontSize:13, marginBottom:8 }}>
                  ⚠️ This server has existing content
                </div>
                <div style={{ fontSize:12, color:"var(--text2)", fontFamily:"'JetBrains Mono',monospace", lineHeight:1.6, marginBottom:10 }}>
                  Detected: {preview.establishment_signals?.channels} channels,
                  {" "}{preview.establishment_signals?.custom_roles} custom roles,
                  {" "}{preview.establishment_signals?.members} members.
                  <br />
                  The wizard will NEVER delete existing content, but it WILL:
                  <br />
                  • Create new roles/channels alongside existing ones
                  <br />
                  • Lock welcome and rules channels read-only (members can read but not write/react)
                  <br />
                  • Modify @everyone view permissions on community channels if verification is on
                  <br />
                  • Post a rules message in the rules channel
                  <br /><br />
                  Confirm below to proceed.
                </div>
                <label style={{ display:"flex", alignItems:"center", gap:10, cursor:"pointer", fontSize:13, color:"var(--text)" }}>
                  <input type="checkbox"
                    checked={confirmEstablished}
                    onChange={(e) => setConfirmEstablished(e.target.checked)}
                    style={{ width:18, height:18, cursor:"pointer" }} />
                  I understand and want to apply the template to this established server
                </label>
              </div>
            )}

            <div style={{ display:"flex", gap:10, marginBottom:18 }}>
              <button onClick={() => startApply(true)} disabled={status?.status === "running"}
                style={{ ...C.btnSecondary, fontSize:13 }}>Dry run</button>
              <button onClick={() => startApply(false)}
                disabled={status?.status === "running" || (preview?.is_established && !confirmEstablished)}
                style={{
                  ...C.btnPrimary, fontSize:13,
                  opacity: (status?.status === "running" || (preview?.is_established && !confirmEstablished)) ? 0.4 : 1,
                }}>
                Apply for real
              </button>
            </div>

            {applyError && <div style={{ color:"var(--red)", marginBottom:12 }}>Error: {applyError}</div>}

            {status && (
              <div>
                <div style={{ display:"flex", alignItems:"center", gap:10, marginBottom:12 }}>
                  <div style={{
                    fontSize:13, fontWeight:700,
                    color: status.status === "done" ? "var(--green)" :
                           status.status === "error" ? "var(--red)" :
                           status.status === "running" ? "var(--cyan)" : "var(--text3)",
                  }}>
                    {status.status?.toUpperCase()}{status.dry_run ? " (DRY RUN)" : ""}
                  </div>
                  {status.summary && (
                    <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>
                      created: {status.summary.created} · reused: {status.summary.reused} · failed: {status.summary.failed}
                    </div>
                  )}
                </div>

                {status.error && <div style={{ color:"var(--red)", marginBottom:12, fontFamily:"'JetBrains Mono',monospace", fontSize:12 }}>{status.error}</div>}

                <div style={{ maxHeight:340, overflowY:"auto", border:"1px solid var(--border)", borderRadius:8, padding:"8px 12px", fontFamily:"'JetBrains Mono',monospace", fontSize:11 }}>
                  {status.steps?.map((s, i) => (
                    <div key={i} style={{ display:"flex", justifyContent:"space-between", alignItems:"center", padding:"3px 0", borderBottom: i === status.steps.length - 1 ? "none" : "1px solid var(--border)" }}>
                      <span style={{ color:"var(--text2)" }}>{s.label}</span>
                      <span style={{
                        color: s.status === "done" ? "var(--green)" :
                               s.status === "error" ? "var(--red)" :
                               s.status === "running" ? "var(--cyan)" : "var(--text3)",
                      }}>
                        {s.status}{s.detail ? ` · ${s.detail}` : ""}
                      </span>
                    </div>
                  ))}
                  {(!status.steps || status.steps.length === 0) && (
                    <div style={{ color:"var(--text3)", padding:"6px 0" }}>Waiting for bot…</div>
                  )}
                </div>

                {status.notes?.length > 0 && (
                  <div style={{ marginTop:14, padding:"12px 14px", background:"rgba(0,245,212,0.04)", border:"1px solid var(--border)", borderRadius:8 }}>
                    <div style={{ fontSize:12, fontWeight:700, color:"var(--cyan)", marginBottom:8, fontFamily:"'Outfit',sans-serif" }}>
                      Notes
                    </div>
                    {status.notes.map((note, i) => (
                      <div key={i} style={{ fontSize:12, color:"var(--text2)", marginBottom:6, lineHeight:1.5, fontFamily:"'JetBrains Mono',monospace", whiteSpace:"pre-wrap" }}>
                        {note}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div style={{ display:"flex", justifyContent:"space-between", marginTop:20 }}>
              <button onClick={() => setStep(s => Math.max(0, s - 1))}
                style={{ ...C.btnSecondary, fontSize:13 }}>← Back</button>
              <div />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ToggleRow({ label, desc, value, onChange }) {
  return (
    <div style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", gap:12, padding:"8px 0" }}>
      <div style={{ flex:1 }}>
        <div style={{ fontSize:13, color:"var(--text)", fontFamily:"'Outfit',sans-serif", fontWeight:600 }}>{label}</div>
        <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>{desc}</div>
      </div>
      <button onClick={() => onChange(!value)}
        style={{
          width:46, height:24, borderRadius:12,
          background: value ? "var(--cyan)" : "var(--bg3)",
          border: "1px solid var(--border)",
          position:"relative", cursor:"pointer",
          transition:"background 0.2s",
        }}>
        <div style={{
          position:"absolute", top:2,
          left: value ? 24 : 2,
          width:18, height:18, borderRadius:"50%",
          background: "white",
          transition:"left 0.2s",
        }} />
      </button>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div style={{ ...C.card, padding:"10px 12px", textAlign:"center" }}>
      <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", textTransform:"uppercase" }}>{label}</div>
      <div style={{ fontSize:18, color:"var(--cyan)", fontFamily:"'Orbitron',sans-serif", fontWeight:800, marginTop:4 }}>{value ?? "—"}</div>
    </div>
  );
}

// ── Dev: Leaderboard Blacklist (sub-component of DB Tools) ────────────────────
function LeaderboardBlacklist() {
  const [list, setList] = useState([]);
  const [streamerInput, setStreamerInput] = useState("");
  const [reasonInput, setReasonInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch("/api/dev/leaderboard-blacklist");
      setList(data.blacklist || []);
      if (data.error) setError(data.error);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const add = async () => {
    const name = streamerInput.trim();
    if (!name) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch("/api/dev/leaderboard-blacklist", {
        method: "POST",
        body: JSON.stringify({ streamer_name: name, reason: reasonInput.trim() || null }),
      });
      setStreamerInput("");
      setReasonInput("");
      await load();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const remove = async (name) => {
    setError(null);
    try {
      await apiFetch(`/api/dev/leaderboard-blacklist/${encodeURIComponent(name)}`, { method: "DELETE" });
      await load();
    } catch (e) {
      setError(e.message || String(e));
    }
  };

  return (
    <div style={{ ...C.card, padding:"14px 18px" }}>
      <div style={{ display:"flex", gap:8, flexWrap:"wrap", alignItems:"center", marginBottom:12 }}>
        <CyanInput
          type="text"
          placeholder="Streamer name"
          value={streamerInput}
          onChange={(e) => setStreamerInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") add(); }}
          style={{ flex:"1 1 180px", minWidth:140 }}
        />
        <CyanInput
          type="text"
          placeholder="Reason (optional)"
          value={reasonInput}
          onChange={(e) => setReasonInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") add(); }}
          style={{ flex:"2 1 280px", minWidth:200 }}
        />
        <button onClick={add} disabled={submitting || !streamerInput.trim()}
          style={{ ...C.btnPrimary, fontSize:12, opacity: (submitting || !streamerInput.trim()) ? 0.5 : 1 }}>
          {submitting ? "Adding…" : "Add"}
        </button>
      </div>

      {error && (
        <div style={{ fontSize:12, color:"var(--red)", fontFamily:"'JetBrains Mono',monospace", marginBottom:8 }}>
          Error: {error}
        </div>
      )}

      <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:6 }}>
        {loading ? "Loading…" : (list.length === 0 ? "No streamers blacklisted." : `${list.length} blacklisted`)}
      </div>

      {list.length > 0 && (
        <div style={{ borderTop:"1px solid var(--border)", maxHeight:300, overflowY:"auto" }}>
          {list.map(item => (
            <div key={item.streamer_name} style={{ display:"grid", gridTemplateColumns:"1fr 2fr auto", gap:14, fontSize:11, padding:"6px 32px 6px 4px", borderBottom:"1px solid var(--border)", alignItems:"center", fontFamily:"'JetBrains Mono',monospace" }}>
              <a href={`https://twitch.tv/${item.streamer_name}`} target="_blank" rel="noreferrer" style={{ color:"var(--text)", textDecoration:"none", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", minWidth:0 }}>
                {item.streamer_name}
              </a>
              <div style={{ color:"var(--text3)", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", minWidth:0 }}>
                {item.reason || <span style={{ color:"var(--text3)", opacity:0.5 }}>—</span>}
              </div>
              <button onClick={() => remove(item.streamer_name)}
                style={{ ...C.btnDanger, fontSize:11, padding:"4px 10px" }}>
                Remove
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Dev: DB Tools Tab ─────────────────────────────────────────────────────────
function DbToolsTab() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState({});
  const [results, setResults] = useState({});
  const [trimDays, setTrimDays] = useState(30);

  const load = useCallback(async () => {
    setLoading(true);
    try { setStatus(await apiFetch("/api/dev/db-tools")); }
    catch(e) { console.error(e); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const run = async (action, extra = {}) => {
    setRunning(p => ({ ...p, [action]: true }));
    try {
      const res = await apiFetch("/api/dev/db-tools", { method:"POST", body: JSON.stringify({ action, ...extra }) });
      setResults(p => ({ ...p, [action]: res.message }));
      load();
    } catch(e) { setResults(p => ({ ...p, [action]: "Error: " + e.message })); }
    finally { setRunning(p => ({ ...p, [action]: false })); }
  };

  if (loading) return <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div>;

  const ToolCard = ({ title, sub, count, action, label, extra, danger }) => (
    <div style={{ ...C.card, display:"flex", justifyContent:"space-between", alignItems:"center", padding:"14px 18px" }}>
      <div>
        <div style={{ fontSize:14, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>{title}</div>
        {sub && <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>{sub}</div>}
        {count !== undefined && <div style={{ fontSize:11, color: count > 0 ? "var(--yellow)" : "var(--green)", marginTop:3, fontFamily:"'JetBrains Mono',monospace" }}>{count} {count === 1 ? "item" : "items"}</div>}
        {results[action] && <div style={{ fontSize:11, color:"var(--cyan)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>✓ {results[action]}</div>}
      </div>
      <button onClick={() => run(action, extra)} disabled={running[action]} style={{ ...(danger ? C.btnDanger : C.btnSecondary), fontSize:12, opacity: running[action] ? 0.5 : 1 }}>
        {running[action] ? "Running…" : label}
      </button>
    </div>
  );

  return (
    <div>
      <PageHeader title="DB Tools" subtitle="Dev-only database maintenance utilities" />
      <div style={{ display:"flex", flexDirection:"column", gap:10 }}>
        <ToolCard
          title="Orphaned Notification Messages"
          sub="notification_messages rows where streamer is no longer monitored"
          count={status?.orphaned_notification_messages?.length}
          action="clear_orphaned_notifications"
          label="Clear"
          danger
        />
        <ToolCard
          title="Orphaned Permission Issues"
          sub="permission_issues rows for channels no longer in use"
          count={status?.orphaned_permission_issues?.length}
          action="clear_orphaned_perms"
          label="Clear"
          danger
        />
        <ToolCard
          title="Bad Streamer Names"
          sub="Streamers stored as full URLs (e.g. https://twitch.tv/name)"
          count={status?.bad_streamer_names?.length}
          action="fix_bad_streamer_names"
          label="Fix"
        />
        <div style={{ ...C.card, display:"flex", justifyContent:"space-between", alignItems:"center", padding:"14px 18px" }}>
          <div>
            <div style={{ fontSize:14, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>Trim Notification Log</div>
            <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>Currently {status?.notification_log_rows?.toLocaleString()} rows</div>
            {results["trim_notification_log"] && <div style={{ fontSize:11, color:"var(--cyan)", marginTop:4 }}>✓ {results["trim_notification_log"]}</div>}
          </div>
          <div style={{ display:"flex", alignItems:"center", gap:8 }}>
            <CyanInput type="number" min={1} max={365} value={trimDays} onChange={e=>setTrimDays(parseInt(e.target.value)||30)} style={{ width:70 }} />
            <span style={{ fontSize:11, color:"var(--text3)" }}>days</span>
            <button onClick={() => run("trim_notification_log", { days: trimDays })} disabled={running["trim_notification_log"]} style={{ ...C.btnDanger, fontSize:12 }}>
              {running["trim_notification_log"] ? "Running…" : "Trim"}
            </button>
          </div>
        </div>
        <ToolCard
          title="Clear Live Streamers Memory"
          sub="Wipes the in-memory live_streamers set — next EventSub event or check will repopulate"
          action="clear_live_streamers"
          label="Clear"
          danger
        />
        <ToolCard
          title="Force EventSub Sync"
          sub="Re-register stream.online / stream.offline subscriptions for all monitored streamers"
          action="sync_eventsub"
          label="Sync Now"
        />

        {/* ── Stream Hours Tracking section ─────────────────────────────── */}
        <div style={{ marginTop:20, marginBottom:8, fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:14, color:"var(--text)", textShadow:"0 0 14px rgba(0,245,212,0.3), 0 0 28px rgba(0,245,212,0.1)" }}>
          ⏱️ Stream Hours Tracking
        </div>
        <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:8 }}>
          Polling health check runs every 15 min — closes orphan rows when Twitch reports streamers as offline
        </div>

        <div style={{ ...C.card, padding:"14px 18px" }}>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom: (status?.live_streamers?.length ?? 0) > 0 ? 12 : 0 }}>
            <div>
              <div style={{ fontSize:14, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>Currently Live Streamers</div>
              <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>
                In-memory <code>live_streamers</code> set with hours-live calculated from EventSub start times
              </div>
              <div style={{ fontSize:11, color: (status?.live_streamers?.length ?? 0) > 0 ? "var(--cyan)" : "var(--text3)", marginTop:3, fontFamily:"'JetBrains Mono',monospace" }}>
                {status?.live_streamers?.length ?? 0} live now
              </div>
            </div>
          </div>
          {(status?.live_streamers?.length ?? 0) > 0 && (
            <div style={{ display:"flex", flexDirection:"column", gap:4, maxHeight:280, overflowY:"auto", borderTop:"1px solid var(--border)", paddingTop:10 }}>
              {status.live_streamers.map(s => (
                <div key={s.streamer_name} style={{ display:"grid", gridTemplateColumns:"1fr 100px", gap:14, alignItems:"center", padding:"4px 32px 4px 0", fontSize:12 }}>
                  <a href={`https://twitch.tv/${s.streamer_name}`} target="_blank" rel="noreferrer" style={{ color:"var(--text)", textDecoration:"none", fontFamily:"'Outfit',sans-serif" }}>
                    {s.streamer_name}
                  </a>
                  <span style={{ color: s.hours_live === null ? "var(--text3)" : "var(--cyan)", fontFamily:"'JetBrains Mono',monospace", textAlign:"right" }}>
                    {s.hours_live === null ? "—" : `${s.hours_live}h live`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={{ ...C.card, padding:"14px 18px" }}>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom: (status?.recent_orphan_closures?.length ?? 0) > 0 ? 12 : 0 }}>
            <div>
              <div style={{ fontSize:14, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>Recent Orphan Closures</div>
              <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>
                Streams the 15-min health poll detected as offline and closed (missed EventSub offline events)
              </div>
              <div style={{ fontSize:11, color: (status?.recent_orphan_closures?.length ?? 0) > 0 ? "var(--yellow)" : "var(--green)", marginTop:3, fontFamily:"'JetBrains Mono',monospace" }}>
                {status?.recent_orphan_closures?.length ?? 0} closure{(status?.recent_orphan_closures?.length ?? 0) === 1 ? "" : "s"} since last restart
              </div>
            </div>
          </div>
          {(status?.recent_orphan_closures?.length ?? 0) > 0 && (
            <div style={{ display:"flex", flexDirection:"column", gap:4, maxHeight:280, overflowY:"auto", borderTop:"1px solid var(--border)", paddingTop:10 }}>
              {status.recent_orphan_closures.map((c, i) => (
                <div key={i} style={{ display:"grid", gridTemplateColumns:"1fr 220px", gap:14, alignItems:"center", padding:"4px 32px 4px 0", fontSize:12 }}>
                  <a href={`https://twitch.tv/${c.streamer_name}`} target="_blank" rel="noreferrer" style={{ color:"var(--text)", textDecoration:"none", fontFamily:"'Outfit',sans-serif" }}>
                    {c.streamer_name}
                  </a>
                  <div style={{ display:"flex", gap:8, alignItems:"center", justifyContent:"flex-end", fontFamily:"'JetBrains Mono',monospace" }}>
                    {c.hours_live_at_close !== null && c.hours_live_at_close !== undefined && (
                      <span style={{ color:"var(--cyan)" }}>{c.hours_live_at_close}h live</span>
                    )}
                    <span style={{ color:"var(--text3)", fontSize:10 }}>{c.closed_at}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={{ ...C.card, display:"flex", justifyContent:"space-between", alignItems:"center", padding:"14px 18px" }}>
          <div>
            <div style={{ fontSize:14, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>Open Stream Rows</div>
            <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>
              <code>global_stream_events</code> rows with NULL <code>ended_at</code> — currently-live + orphans from missed offlines
            </div>
            <div style={{ fontSize:11, color: (status?.open_session_count ?? 0) > (status?.live_streamers?.length ?? 0) ? "var(--yellow)" : "var(--green)", marginTop:3, fontFamily:"'JetBrains Mono',monospace" }}>
              {status?.open_session_count ?? 0} open row{(status?.open_session_count ?? 0) === 1 ? "" : "s"}
              {(status?.open_session_count ?? 0) > (status?.live_streamers?.length ?? 0) && (status?.live_streamers?.length !== undefined) && (
                <span style={{ color:"var(--text3)" }}> ({(status.open_session_count - status.live_streamers.length)} likely orphans)</span>
              )}
            </div>
          </div>
        </div>

        <div style={{ ...C.card, display:"flex", justifyContent:"space-between", alignItems:"center", padding:"14px 18px" }}>
          <div>
            <div style={{ fontSize:14, fontWeight:600, color:"var(--text)", fontFamily:"'Outfit',sans-serif" }}>Reset All Saved Hours</div>
            <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginTop:2 }}>
              Sets every <code>ended_at</code> to NULL in both stream_events tables. Stream counts preserved; hours/longest go to 0.
            </div>
            {results["reset_hours"] && <div style={{ fontSize:11, color:"var(--cyan)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>✓ {results["reset_hours"]}</div>}
          </div>
          <button
            onClick={() => { if (window.confirm("Reset ALL saved hours? Stream counts will be preserved but every hours/longest value resets to 0.")) run("reset_hours"); }}
            disabled={running["reset_hours"]}
            style={{ ...C.btnDanger, fontSize:12, opacity: running["reset_hours"] ? 0.5 : 1 }}
          >
            {running["reset_hours"] ? "Running…" : "Reset Hours"}
          </button>
        </div>

        {/* ── Leaderboard Blacklist section ──────────────────────────────── */}
        <div style={{ marginTop:20, marginBottom:8, fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:14, color:"var(--text)", textShadow:"0 0 14px rgba(0,245,212,0.3), 0 0 28px rgba(0,245,212,0.1)" }}>
          📛 Leaderboard Blacklist
        </div>
        <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:8 }}>
          Streamers excluded from hours/longest leaderboards (still shown in stream count). Use for channels with rerun content inflating their live time.
        </div>
        <LeaderboardBlacklist />

        {/* ── Stream Events log section ─────────────────────────────────── */}
        <div style={{ marginTop:20, marginBottom:8, fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:14, color:"var(--text)", textShadow:"0 0 14px rgba(0,245,212,0.3), 0 0 28px rgba(0,245,212,0.1)" }}>
          📋 Stream Events Log
        </div>
        <div style={{ fontSize:11, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", marginBottom:8 }}>
          Raw events from global_stream_events. Current + previous month retained. Times shown in your local timezone.
        </div>
        <StreamEventsViewer />

        {status?.bad_streamer_names?.length > 0 && (
          <div style={{ ...C.card, marginTop:4 }}>
            <div style={{ fontSize:12, color:"var(--yellow)", fontFamily:"'JetBrains Mono',monospace", marginBottom:8 }}>Bad names found:</div>
            {status.bad_streamer_names.map(b => (
              <div key={b.name} style={{ fontSize:12, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>
                {b.name} <span style={{ color:"var(--text3)" }}>({b.guild_count} guild{b.guild_count !== 1 ? "s" : ""})</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AdminAuditLogTab() {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiFetch("/api/admin/audit-log")
      .then(setEntries)
      .catch(e => console.error(e))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <div style={{ marginBottom:16 }}>
        <h2 style={{ fontFamily:"'Orbitron',sans-serif", fontWeight:800, fontSize:22, color:"var(--text)", margin:0, textShadow:"0 0 20px rgba(0,245,212,0.4)" }}>Admin Audit Log</h2>
        <div style={{ fontSize:12, color:"var(--text3)", marginTop:4 }}>Last 100 admin actions on servers they don't own. Owner-only.</div>
      </div>
      {loading ? <Spinner /> : entries.length === 0 ? (
        <div style={{ color:"var(--text3)", fontSize:13, padding:"40px 0", textAlign:"center" }}>No admin actions recorded yet.</div>
      ) : (
        <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
          {entries.map(e => (
            <div key={e.id} style={{ ...C.card, padding:"10px 14px", display:"flex", alignItems:"flex-start", gap:12 }}>
              <div style={{ flex:1, minWidth:0 }}>
                <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:4, flexWrap:"wrap" }}>
                  <span style={{ fontSize:11, fontFamily:"'JetBrains Mono',monospace", color:"var(--cyan)", fontWeight:600 }}>{e.admin_username}</span>
                  <span style={{ fontSize:10, padding:"1px 6px", borderRadius:4, fontFamily:"'JetBrains Mono',monospace", fontWeight:700,
                    background: e.method==="DELETE" ? "rgba(255,77,109,0.15)" : e.method==="POST" ? "rgba(0,245,212,0.1)" : "rgba(245,180,50,0.1)",
                    color: e.method==="DELETE" ? "var(--red)" : e.method==="POST" ? "var(--cyan)" : "#f5b432"
                  }}>{e.method}</span>
                  <span style={{ fontSize:11, color:"var(--text2)", fontFamily:"'JetBrains Mono',monospace", wordBreak:"break-all" }}>{e.endpoint}</span>
                </div>
                <div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>
                  Guild: {e.guild_id} · {new Date(e.timestamp).toLocaleString()}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [user, setUser] = useState(null);
  const [guilds, setGuilds] = useState([]);
  const [activeGuild, setActiveGuild] = useState(null);
  const [activeTab, setActiveTab] = useState("settings");
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loggedIn, setLoggedIn] = useState(false);
  const [navDrawerOpen, setNavDrawerOpen] = useState(false);
  const isMobile = useIsMobile();
  const [viewMode, setViewMode] = useState("dev"); // "dev" | "admin" | "user"

  useEffect(()=>{
    // Switch to rewards tab if returning from Twitch OAuth
    const params = new URLSearchParams(window.location.search);
    if (params.get("twitch_connected")) {
      setActiveTab("rewards");
      window.history.replaceState({}, "", "/app/");
    }
    // Cookie is sent automatically — just try /api/me to check if logged in
    apiFetch("/api/me").then(data=>{
      if (!data || !data.guilds) return;
      setUser(data); const g = data.guilds||[]; setGuilds(g);
      if (g.length) { const saved = localStorage.getItem("ep_last_guild"); const match = g.find(x=>x.id===saved); setActiveGuild(match?match.id:g[0].id); }
      setLoggedIn(true);
    }).catch(()=>{ /* not logged in */ }).finally(()=>setLoading(false));
  },[]);

  const logout = async () => {
    try { await apiFetch("/auth/logout", { method:"POST" }); } catch(e) {}
    setLoggedIn(false); setUser(null);
  };
  const switchGuild = (id) => { setActiveGuild(id); localStorage.setItem("ep_last_guild",id); setDropdownOpen(false); setActiveTab("settings"); };

  if (loading) return <div style={{ height:"100vh", background:"var(--bg)", display:"flex", alignItems:"center", justifyContent:"center" }}><Spinner size={32} /></div>;
  if (!loggedIn) return <LoginScreen />;

  const guild = guilds.find(g=>g.id===activeGuild)||guilds[0]||{ id:"", name:"..." };
  const notificationsTabs = [
    { id:"streamers",      icon:"/app/icons/streams.png", label:"Streams"           },
    { id:"notiflog",       icon:"/app/icons/log.png", label:"Notification Log"  },
  ];
  const twitchTabs = [
    { id:"twitch",         icon:"/app/icons/message.png", label:"Chat Commands"     },
    { id:"rewards",        icon:"/app/icons/rewards.png", label:"Channel Rewards"   },
  ];
  const communityTabs = [
    { id:"roles",          icon:"/app/icons/reactionroles.png", label:"Reaction Roles"    },
    { id:"birthdays",      icon:"/app/icons/birthday.png",      label:"Birthdays"         },
    { id:"welcomegoodbye", icon:"/app/icons/welcome.png",       label:"Welcome & Goodbye" },
    { id:"voicerooms",     icon:"/app/icons/vc.png",            label:"Voice Rooms"       },
  ];
  const moderationTabs = [
    { id:"safety",         icon:"/app/icons/shield.png", label:"Safety"            },
    { id:"cleanuprules",   icon:"/app/icons/cleanup.png", label:"Cleanup Rules"     },
  ];
  const serverConfigTabs = [
    { id:"settings",       icon:"/app/icons/gear.png", label:"General Settings"  },
    { id:"statstab",       icon:"/app/icons/stats.png", label:"Stats Channel"     },
  ];
  const setupWizardTabs = [
    { id:"setupwizard",    icon:"/app/icons/wizard.png", label:"Set Up Server"     },
  ];
  const isActuallyDev = user?.is_dev === true;
  const isAdmin = user?.is_admin === true;
  const effectivelyDev   = isActuallyDev && viewMode === "dev";
  const effectivelyAdmin = isActuallyDev && viewMode === "admin";
  const devTabs = effectivelyDev ? [
    { id:"globalstats",    icon:"/app/icons/globe.png", label:"Global Stats"      },
    { id:"dbtools",        icon:"/app/icons/tools.png", label:"DB Tools"          },
    { id:"auditlog",       icon:"/app/icons/log.png",   label:"Admin Audit Log"   },
  ] : (effectivelyAdmin || isAdmin) ? [
    { id:"globalstats",    icon:"/app/icons/globe.png", label:"Global Stats"      },
  ] : [];
  const tabs = [...notificationsTabs, ...twitchTabs, ...communityTabs, ...moderationTabs, ...serverConfigTabs, ...setupWizardTabs];

  return (
    <div style={{ height:"100vh", width:"100vw", background:"var(--bg)", color:"var(--text)", fontFamily:"'Outfit',sans-serif", display:"flex", flexDirection:"column", overflow:"hidden", position:"relative" }}>
      {/* Particle background */}
      <ParticleCanvas />
      {/* Content above orbs */}
      <div style={{ position:"relative", zIndex:1, display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>

      {/* Top Bar */}
      <div style={{ height:52, background:"linear-gradient(180deg, rgba(14,20,28,0.99) 0%, rgba(10,15,22,0.99) 100%)", borderBottom:"1px solid rgba(0,245,212,0.15)", display:"flex", alignItems:"center", padding:"0 18px 0 10px", gap:12, flexShrink:0, boxShadow:"0 1px 12px rgba(0,245,212,0.07), 0 2px 8px rgba(0,0,0,0.4)" }}>
        {/* Logo */}
        <div style={{ display:"flex", alignItems:"center", gap:10, marginRight:8 }}>
          <img src="/app/protocol.png" alt="ExcelProtocol" style={{ width:55, height:55, display:"block", flexShrink:0 }} />
          <div>
            <div style={{ fontWeight:800, fontSize:13, letterSpacing:0.5, color:"var(--text)", textShadow:"0 0 12px rgba(0,245,212,0.35)" }}>ExcelProtocol</div>
            <div style={{ fontSize:9, color:"var(--cyan2)", letterSpacing:1.5, fontFamily:"'JetBrains Mono',monospace", lineHeight:1, textShadow:"0 0 8px rgba(0,196,170,0.6)" }}>DASHBOARD</div>
          </div>
        </div>

        <div style={{ width:1, height:24, background:"var(--border)", margin:"0 4px" }} />

        {/* Guild Switcher */}
        <div style={{ position:"relative" }}>
          <button onClick={()=>setDropdownOpen(o=>!o)} style={{ display:"flex", alignItems:"center", gap:8, padding:"5px 10px 5px 7px", borderRadius:7, border:"1px solid var(--border)", background:"var(--bg2)", color:"var(--text)", cursor:"pointer", fontSize:13, fontFamily:"'Outfit',sans-serif", fontWeight:500 }}>
            <GuildAvatar guild={guild} size={22} />
            {!isMobile && <span>{guild.name}</span>}
            {!isMobile && guild.approximate_member_count && <span style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{guild.approximate_member_count.toLocaleString()}</span>}
            <span style={{ color:"var(--text3)", fontSize:9, marginLeft:2 }}>▼</span>
          </button>
          {dropdownOpen && guilds.length>1 && (
            <div style={{ position:"absolute", top:"calc(100% + 6px)", left:0, background:"linear-gradient(135deg, rgba(16,23,33,0.99) 0%, rgba(11,16,24,0.99) 100%)", border:"1px solid rgba(0,245,212,0.22)", borderRadius:10, padding:6, minWidth:220, zIndex:100, boxShadow:"0 8px 32px rgba(0,0,0,0.7), 0 0 0 1px rgba(0,245,212,0.04), inset 0 1px 0 rgba(0,245,212,0.09)", maxHeight:"calc(100vh - 80px)", overflowY:"auto" }}>
              {/* Own servers — always shown */}
              {(effectivelyAdmin || isAdmin ? guilds.filter(g => !g.admin_access) : guilds).map(g=>(
                <button key={g.id} onClick={()=>switchGuild(g.id)} style={{ display:"flex", alignItems:"center", gap:10, width:"100%", padding:"8px 10px", borderRadius:7, border:"none", background:activeGuild===g.id?"var(--cyan-dim)":"transparent", color:activeGuild===g.id?"var(--cyan)":"var(--text)", cursor:"pointer", fontSize:13, fontFamily:"'Outfit',sans-serif", textAlign:"left" }}>
                  <GuildAvatar guild={g} size={24} />
                  <div style={{ flex:1 }}>
                    <div style={{ fontSize:13, fontWeight:500, fontFamily:"'Outfit',sans-serif" }}>{g.name}</div>
                    {g.approximate_member_count&&<div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{g.approximate_member_count.toLocaleString()} members</div>}
                  </div>
                  {activeGuild===g.id&&<span style={{ color:"var(--cyan)", fontSize:12 }}>✓</span>}
                </button>
              ))}
              {/* Admin access section */}
              {guilds.some(g => g.admin_access) && (effectivelyAdmin || isAdmin) && <>
                <div style={{ fontSize:9, color:"rgba(245,180,50,0.8)", textTransform:"uppercase", letterSpacing:1.5, padding:"8px 10px 4px", fontFamily:"'JetBrains Mono',monospace", borderTop:"1px solid var(--border)", marginTop:4 }}>Admin Access</div>
                {guilds.filter(g => g.admin_access).map(g=>(
                  <button key={g.id} onClick={()=>switchGuild(g.id)} style={{ display:"flex", alignItems:"center", gap:10, width:"100%", padding:"8px 10px", borderRadius:7, border:"none", background:activeGuild===g.id?"rgba(245,180,50,0.1)":"transparent", color:activeGuild===g.id?"#f5b432":"var(--text2)", cursor:"pointer", fontSize:13, fontFamily:"'Outfit',sans-serif", textAlign:"left" }}>
                    <GuildAvatar guild={g} size={24} />
                    <div style={{ flex:1 }}>
                      <div style={{ fontSize:13, fontWeight:500, fontFamily:"'Outfit',sans-serif" }}>{g.name}</div>
                      {g.approximate_member_count&&<div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{g.approximate_member_count.toLocaleString()} members</div>}
                    </div>
                    <span style={{ fontSize:12 }}>🔑</span>
                    {activeGuild===g.id&&<span style={{ color:"#f5b432", fontSize:12 }}>✓</span>}
                  </button>
                ))}
              </>}
            </div>
          )}
        </div>

        {/* Right side */}
        <div style={{ marginLeft:"auto", display:"flex", alignItems:"center", gap:10 }}>
          {isActuallyDev && !isMobile && (
            <button
              onClick={() => { setViewMode(m => m==="dev"?"admin":m==="admin"?"user":"dev"); setActiveTab("settings"); }}
              title="Cycle view: DEV → ADMIN → USER"
              style={{ padding:"4px 10px", borderRadius:7, border:`1px solid ${viewMode==="dev" ? "rgba(245,200,66,0.4)" : viewMode==="admin" ? "rgba(245,180,50,0.4)" : "var(--border)"}`, background: viewMode==="dev" ? "rgba(245,200,66,0.1)" : viewMode==="admin" ? "rgba(245,180,50,0.1)" : "transparent", color: viewMode==="dev" ? "#f5c842" : viewMode==="admin" ? "#f5b432" : "var(--text3)", fontSize:11, cursor:"pointer", fontFamily:"'JetBrains Mono',monospace", fontWeight:600, letterSpacing:0.5 }}
            >
              {viewMode==="dev" ? "DEV VIEW" : viewMode==="admin" ? "ADMIN VIEW" : "USER VIEW"}
            </button>
          )}
          {isAdmin && !isMobile && (
            <div style={{ padding:"4px 10px", borderRadius:7, border:"1px solid rgba(245,180,50,0.4)", background:"rgba(245,180,50,0.1)", color:"#f5b432", fontSize:11, fontFamily:"'JetBrains Mono',monospace", fontWeight:600, letterSpacing:0.5 }}>
              ADMIN
            </div>
          )}
          {!isMobile && <div style={{ display:"flex", alignItems:"center", gap:6, padding:"3px 10px 3px 6px", borderRadius:20, border:"1px solid var(--border)", background:"var(--bg2)" }}>
            <div style={{ width:6, height:6, borderRadius:"50%", background:"var(--green)", animation:"pulse 2s ease infinite", boxShadow:"0 0 6px rgba(57,217,138,0.8), 0 0 12px rgba(57,217,138,0.4)" }} />
            <span style={{ fontSize:10, color:"var(--green)", fontFamily:"'JetBrains Mono',monospace", letterSpacing:0.5, textShadow:"0 0 8px rgba(57,217,138,0.7)" }}>ONLINE</span>
          </div>}
          {isMobile && <button
            onClick={() => setNavDrawerOpen(true)}
            style={{ background:"transparent", border:"1px solid var(--border2)", borderRadius:8, color:"var(--text2)", padding:"10px 14px", cursor:"pointer", fontSize:20, lineHeight:1, minWidth:44, minHeight:44, display:"flex", alignItems:"center", justifyContent:"center" }}
          >☰</button>}
          <UserAvatar user={user} size={28} />
          {!isMobile && <span style={{ fontSize:13, color:"var(--text2)", fontWeight:500, fontFamily:"'Outfit',sans-serif" }}>{user?.username}</span>}
          {!isMobile && <button onClick={logout} style={{ ...C.btnSecondary, padding:"4px 10px", fontSize:11 }}>Log out</button>}
        </div>
      </div>

      {/* Body */}
      <div style={{ display:"flex", flex:1, minHeight:0, overflow:"hidden" }}>

        {/* Sidebar — hidden on mobile */}
        <div className="mob-sidebar" style={{ width:200, background:"linear-gradient(180deg, rgba(12,18,26,0.99) 0%, rgba(9,14,21,0.99) 100%)", borderRight:"1px solid rgba(0,245,212,0.12)", padding:"14px 8px", flexShrink:0, display:"flex", flexDirection:"column", gap:2, overflowY:"auto", boxShadow:"inset -1px 0 0 rgba(0,245,212,0.04)", position:"relative" }}>
          <SidebarParticles />
          {guilds.length===1 && (
            <div style={{ display:"flex", alignItems:"center", gap:8, padding:"8px 6px 12px", marginBottom:8, borderBottom:"1px solid var(--border)" }}>
              <GuildAvatar guild={guild} size={30} />
              <div>
                <div style={{ fontSize:12, fontWeight:600, fontFamily:"'Outfit',sans-serif", color:"var(--text)" }}>{guild.name}</div>
                {guild.approximate_member_count&&<div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>{guild.approximate_member_count.toLocaleString()} members</div>}
              </div>
            </div>
          )}
          <div style={{ position:"relative", zIndex:1, fontSize:9, color:"var(--text3)", textTransform:"uppercase", letterSpacing:1.5, padding:"0 6px 6px", fontFamily:"'JetBrains Mono',monospace" }}>Navigation</div>
          <NavGroup icon="/app/icons/gear.png" label="Server Config" activeTab={activeTab} tabs={serverConfigTabs} onSelect={setActiveTab} />
<NavGroup icon="/app/icons/bell.png" label="Notifications" activeTab={activeTab} tabs={notificationsTabs} onSelect={setActiveTab} />
          <NavGroup icon="/app/icons/twitch.png" label="Twitch" activeTab={activeTab} tabs={twitchTabs} onSelect={setActiveTab} />
          <NavGroup icon="/app/icons/people.png" label="Community" activeTab={activeTab} tabs={communityTabs} onSelect={setActiveTab} />
          <NavGroup icon="/app/icons/shield.png" label="Moderation" activeTab={activeTab} tabs={moderationTabs} onSelect={setActiveTab} />
          
          <NavGroup icon="/app/icons/wizard.png" label="Setup Wizard" activeTab={activeTab} tabs={setupWizardTabs} onSelect={setActiveTab} />
          <NavItem key="suggestions" icon="/app/icons/bulb.png" label="Contact" active={activeTab==="suggestions"} onClick={()=>setActiveTab("suggestions")} count={null} />
          {devTabs.length > 0 && (
            <>
              <div style={{ position:"relative", zIndex:1, fontSize:9, color: effectivelyAdmin ? "#f5b432" : "var(--yellow)", textTransform:"uppercase", letterSpacing:1.5, padding:"10px 6px 4px", fontFamily:"'JetBrains Mono',monospace", opacity:0.7 }}>{effectivelyAdmin ? "Admin Only" : "Dev Only"}</div>
              {devTabs.map(t=><NavItem key={t.id} icon={t.icon} label={t.label} active={activeTab===t.id} onClick={()=>setActiveTab(t.id)} count={null} />)}
            </>
          )}
          <div style={{ marginTop:"auto", padding:"0 6px 0", position:"relative", zIndex:1 }}>
            <div style={{ paddingTop:12, borderTop:"1px solid var(--border)" }}>
              <div style={{ fontSize:9, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace", letterSpacing:1 }}>HOSTED ON FLY.IO</div>
              <div style={{ fontSize:10, color:"var(--text3)", marginTop:4, fontFamily:"'JetBrains Mono',monospace" }}>StayExcellent666</div>
            </div>
          </div>
        </div>

        {/* Main content — fills all remaining space */}
        <div className="mob-main-pad" style={{ flex:1, minWidth:0, width:0, padding:"24px 28px", overflowY:"auto" }}>
          {!activeGuild ? <div style={{ display:"flex", justifyContent:"center", padding:60 }}><Spinner /></div> : (
            <>
              {activeTab==="settings"      && <GeneralSettingsTab  guildId={activeGuild} />}
              {activeTab==="setupwizard"   && <SetupWizardTab      guildId={activeGuild} isDev={effectivelyDev} />}
              {activeTab==="statstab"      && <ServerStatsTab      guildId={activeGuild} />}
              {activeTab==="streamers"     && <StreamersTab        guildId={activeGuild} isDev={effectivelyDev} />}
              {activeTab==="roles"         && <ReactionRolesTab    guildId={activeGuild} />}
              {activeTab==="birthdays"     && <BirthdaysTab        guildId={activeGuild} />}
              {activeTab==="welcomegoodbye"&& <WelcomeGoodbyeTab   guildId={activeGuild} />}
              {activeTab==="voicerooms"    && <VoiceRoomsTab       guildId={activeGuild} />}
              {activeTab==="cleanuprules"  && <CleanupRulesTab     guildId={activeGuild} />}
              {activeTab==="twitch"        && <TwitchTab           guildId={activeGuild} isDev={effectivelyDev} />}
              {activeTab==="rewards"       && <ChannelRewardsTab   guildId={activeGuild} />}
              {activeTab==="notiflog"      && <NotifLogTab         guildId={activeGuild} />}
              {activeTab==="safety"        && <SafetyTab           guildId={activeGuild} />}
              {activeTab==="suggestions"   && <SuggestionsTab      guildId={activeGuild} />}
              {activeTab==="globalstats"   && (effectivelyDev || effectivelyAdmin || isAdmin) && <GlobalStatsTab />}
              {activeTab==="dbtools"       && effectivelyDev && <DbToolsTab />}
              {activeTab==="auditlog"      && effectivelyDev && <AdminAuditLogTab />}
            </>
          )}
        </div>
      </div>

      {dropdownOpen && <div onClick={()=>setDropdownOpen(false)} style={{ position:"fixed", inset:0, zIndex:50 }} />}

      {/* Mobile nav drawer */}
      {navDrawerOpen && (
        <div className="mob-nav-drawer" onClick={() => setNavDrawerOpen(false)}>
          <div onClick={e => e.stopPropagation()} style={{ width:280, height:"100%", background:"linear-gradient(180deg, rgba(12,18,26,0.99) 0%, rgba(9,14,21,0.99) 100%)", borderRight:"1px solid rgba(0,245,212,0.15)", padding:"16px 10px", display:"flex", flexDirection:"column", gap:2, overflowY:"auto" }}>
            <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:14, paddingBottom:12, borderBottom:"1px solid var(--border)" }}>
              <div style={{ display:"flex", alignItems:"center", gap:10 }}>
                <UserAvatar user={user} size={32} />
                <div>
                  <div style={{ fontSize:13, fontWeight:600, color:"var(--text)" }}>{user?.username}</div>
                  <div style={{ fontSize:10, color:"var(--text3)", fontFamily:"'JetBrains Mono',monospace" }}>DASHBOARD</div>
                </div>
              </div>
              <button onClick={() => setNavDrawerOpen(false)} style={{ background:"transparent", border:"none", color:"var(--text3)", fontSize:18, cursor:"pointer", padding:4 }}>✕</button>
            </div>
            {guilds.length === 1 && (
              <div style={{ display:"flex", alignItems:"center", gap:8, padding:"6px 6px 10px", marginBottom:8, borderBottom:"1px solid var(--border)" }}>
                <GuildAvatar guild={guild} size={28} />
                <div style={{ fontSize:12, fontWeight:600, color:"var(--text)" }}>{guild?.name}</div>
              </div>
            )}
            <div style={{ fontSize:9, color:"var(--text3)", textTransform:"uppercase", letterSpacing:1.5, padding:"0 6px 6px", fontFamily:"'JetBrains Mono',monospace" }}>Navigation</div>
            <NavGroup large icon="/app/icons/gear.png" label="Server Config" activeTab={activeTab} tabs={serverConfigTabs} onSelect={(id) => { setActiveTab(id); setNavDrawerOpen(false); }} />
<NavGroup large icon="/app/icons/bell.png" label="Notifications" activeTab={activeTab} tabs={notificationsTabs} onSelect={(id) => { setActiveTab(id); setNavDrawerOpen(false); }} />
            <NavGroup large icon="/app/icons/twitch.png" label="Twitch" activeTab={activeTab} tabs={twitchTabs} onSelect={(id) => { setActiveTab(id); setNavDrawerOpen(false); }} />
            <NavGroup large icon="/app/icons/people.png" label="Community" activeTab={activeTab} tabs={communityTabs} onSelect={(id) => { setActiveTab(id); setNavDrawerOpen(false); }} />
            <NavGroup large icon="/app/icons/shield.png" label="Moderation" activeTab={activeTab} tabs={moderationTabs} onSelect={(id) => { setActiveTab(id); setNavDrawerOpen(false); }} />
            
            <NavGroup large icon="/app/icons/wizard.png" label="Setup Wizard" activeTab={activeTab} tabs={setupWizardTabs} onSelect={(id) => { setActiveTab(id); setNavDrawerOpen(false); }} />
            <NavItem large key="suggestions" icon="/app/icons/bulb.png" label="Contact" active={activeTab==="suggestions"} onClick={() => { setActiveTab("suggestions"); setNavDrawerOpen(false); }} count={null} />
            {effectivelyDev && <>
              <div style={{ fontSize:9, color:"var(--cyan)", textTransform:"uppercase", letterSpacing:1.5, padding:"10px 6px 6px", fontFamily:"'JetBrains Mono',monospace" }}>Dev Only</div>
              <NavItem large icon="/app/icons/globe.png" label="Global Stats" active={activeTab==="globalstats"} onClick={() => { setActiveTab("globalstats"); setNavDrawerOpen(false); }} />
              <NavItem large icon="/app/icons/tools.png" label="DB Tools" active={activeTab==="dbtools"} onClick={() => { setActiveTab("dbtools"); setNavDrawerOpen(false); }} />
            </>}
            <div style={{ marginTop:"auto", paddingTop:12, borderTop:"1px solid var(--border)" }}>
              <button onClick={logout} style={{ ...C.btnSecondary, width:"100%", justifyContent:"center" }}>Log out</button>
            </div>
          </div>
        </div>
      )}



      </div>{/* /content wrapper */}
    </div>
  );
}
