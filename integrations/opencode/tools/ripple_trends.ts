import { tool } from "@opencode-ai/plugin"
const base = () => process.env.RIPPLE_TOOL_BASE || ""
const token = () => process.env.RIPPLE_AGENT_TOOL_TOKEN || ""
async function post(path: string, body: unknown) { const r = await fetch(base()+path,{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:JSON.stringify(body)}); const t=await r.text(); if(!r.ok) throw new Error(`Ripple tool failed (${r.status})`); return t }
export default tool({ description:"读取 Ripple 当前热点。只读，不使用社交账号登录态。", args:{ platforms:tool.schema.string().optional(), limit:tool.schema.number().int().min(1).max(12).optional() }, async execute(args){ return post("/api/ripple-agent/tools/trends",{platforms:args.platforms||"",limit:args.limit||6}) } })
