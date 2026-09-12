import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
async function post(body:unknown){const r=await fetch(base()+"/api/ripple-agent/tools/interactions/read",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:JSON.stringify(body)});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}
export default tool({description:"读取 Ripple 互动能力、已同步评论源和互动草稿记录。只读。",args:{operation:tool.schema.string(),platform:tool.schema.string().max(40).optional(),limit:tool.schema.number().int().min(1).max(100).optional()},async execute(args){return post({operation:args.operation,platform:args.platform||"",limit:args.limit||50})}})
