type Draft = { title: string; body: string; tags: string; media: string[]; project_id: string };

// AI 返回时按字段合并，保留等待期间的手工修改和素材顺序。
export function mergeContentDraft(base: Draft, local: Draft, incoming: Draft): Draft {
  const merged = { ...incoming, media: [...incoming.media] };
  for (const field of ['title', 'body', 'tags', 'project_id'] as const) {
    if (local[field] !== base[field]) merged[field] = local[field];
  }
  if (JSON.stringify(local.media) !== JSON.stringify(base.media)) {
    merged.media = [
      ...local.media.filter((path) => !base.media.includes(path) || incoming.media.includes(path)),
      ...incoming.media.filter((path) => !base.media.includes(path) && !local.media.includes(path)),
    ];
  }
  return merged;
}

export function contentDraftChanged(a: Draft, b: Draft): boolean {
  return ['title', 'body', 'tags', 'project_id'].some((field) => a[field as keyof Draft] !== b[field as keyof Draft])
    || JSON.stringify(a.media) !== JSON.stringify(b.media);
}
