/**
 * Force a Content-Disposition to "attachment", keeping only key=value
 * parameters (e.g. the filename). Used by the dashboard proxy for every
 * upstream file it won't let the browser render inline.
 */
export function asAttachment(disposition: string | null): string {
  const params = (disposition ?? '')
    .split(';')
    .slice(1)
    .map((p) => p.trim())
    .filter((p) => /^[\w*-]+=/.test(p) && !/[\r\n]/.test(p));
  return ['attachment', ...params].join('; ');
}
