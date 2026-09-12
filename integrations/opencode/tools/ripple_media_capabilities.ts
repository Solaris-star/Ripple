import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
export default tool({description:"读取 Ripple 当前图片/视频 Provider、默认模型与可选模型。只返回脱敏能力信息，不返回 API 地址、密钥或代理配置。",args:{},async execute(){const r=await fetch(base()+"/api/ripple-agent/tools/media/capabilities",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:"{}"});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}})
