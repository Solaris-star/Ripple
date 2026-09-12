import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
export default tool({description:"读取 Ripple 已连接账号的公开状态与渠道能力。只读，不登录、不提交验证码。",args:{},async execute(){const r=await fetch(base()+"/api/ripple-agent/tools/accounts",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:"{}"});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}})
