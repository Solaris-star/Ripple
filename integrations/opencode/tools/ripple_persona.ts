import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
export default tool({description:"读取 Ripple 中指定账号画像，供选题和创作参考。只读。",args:{name:tool.schema.string().min(1).max(100)},async execute(args){const r=await fetch(base()+"/api/ripple-agent/tools/persona/read",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:JSON.stringify({name:args.name})});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}})
