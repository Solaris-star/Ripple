export type WorkspaceFocus = { contentId?: string; variantId?: string; taskId?: string };

export function workspaceUrl(href: string, page: string, focus: WorkspaceFocus = {}): URL {
  const url = new URL(href);
  url.searchParams.set('page', page);
  for (const key of ['content', 'variant', 'task']) url.searchParams.delete(key);
  if (page === 'contents' && focus.contentId) url.searchParams.set('content', focus.contentId);
  if (page === 'contents' && focus.variantId) url.searchParams.set('variant', focus.variantId);
  if (page === 'publish' && focus.taskId) url.searchParams.set('task', focus.taskId);
  return url;
}
