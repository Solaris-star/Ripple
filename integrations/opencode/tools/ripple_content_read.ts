import { tool } from "@opencode-ai/plugin"
const base=()=>process.env.RIPPLE_TOOL_BASE||"";const token=()=>process.env.RIPPLE_AGENT_TOOL_TOKEN||"";
async function post(body:unknown){const r=await fetch(base()+"/api/ripple-agent/tools/content/read",{method:"POST",headers:{"Content-Type":"application/json","X-Ripple-Agent-Token":token()},body:JSON.stringify(body)});const t=await r.text();if(!r.ok)throw new Error(`Ripple tool failed (${r.status})`);return t}
export default tool({description:"读取 Ripple 内容主稿列表或指定内容。只读。",args:{operation:tool.schema.string(),content_id:tool.schema.string().regex(/^[a-f0-9]{32}$/).optional(),limit:tool.schema.number().int().min(1).max(50).optional()},async execute(args){return post({operation:args.operation,content_id:args.content_id||"",limit:args.limit||20})}})
