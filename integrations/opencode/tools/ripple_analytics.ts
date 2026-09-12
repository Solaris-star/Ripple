import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
export default tool({description:"读取 Ripple 本地发布任务统计与已有回执汇总。只读，不抓取平台、不登录。",args:{},async execute(){const r=await fetch(base()+"/api/ripple-agent/tools/analytics",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:"{}"});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}})
