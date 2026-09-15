// Canvas drawing of Preview JSON v1. Pure: (canvas, preview, viewport) -> pixels.
// One y-flip only, here. Coordinates stay in database units until the transform.

const MARGIN = 0.05;
const frames = new WeakMap(); // Preview/highlight payloads are immutable after fetch.

export function fitViewport(bbox) {
  if (!bbox) return null;
  const [x1, y1, x2, y2] = bbox;
  const w = Math.max(1, x2 - x1), h = Math.max(1, y2 - y1);
  return [x1 - w * MARGIN, y1 - h * MARGIN, x2 + w * MARGIN, y2 + h * MARGIN];
}

export function layerColor(layer, datatype) {
  // Deterministic hue per layer/datatype so the same layer looks the same in every version.
  const hue = ((layer * 47 + datatype * 13) % 360 + 360) % 360;
  return { fill: `hsla(${hue}, 70%, 45%, 0.45)`, stroke: `hsl(${hue}, 70%, 35%)` };
}

/** Draw and return {scale, drawn} — drawn = number of items painted.
 *  highlights: optional list of [x1,y1,x2,y2] dbu boxes outlined on top (changed cells). */
export function draw(canvas, preview, viewport, highlights = null) {
  const dpr = Math.max(1, Math.min(3, globalThis.devicePixelRatio || 1));
  const width = Math.max(1, Math.min(4096, Math.round(canvas.clientWidth > 0 ? canvas.clientWidth * dpr : canvas.width)));
  const height = Math.max(1, Math.min(4096, Math.round(canvas.clientHeight > 0 ? canvas.clientHeight * dpr : canvas.height)));
  const viewKey = JSON.stringify(viewport);
  const prior = frames.get(canvas);
  if (prior && prior.preview === preview && prior.highlights === highlights && prior.viewKey === viewKey
      && canvas.width === width && canvas.height === height && prior.width === width && prior.height === height) {
    return prior.result;
  }
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  const result = paint(canvas, preview, viewport, highlights);
  frames.set(canvas, { preview, highlights, viewKey, width, height, result });
  return result;
}

function paint(canvas, preview, viewport, highlights) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width, height = canvas.height;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, width, height);
  if (!preview || !viewport) return { scale: 0, drawn: 0 };
  const [vx1, vy1, vx2, vy2] = viewport;
  const vw = Math.max(1, vx2 - vx1), vh = Math.max(1, vy2 - vy1);
  const scale = Math.min(width / vw, height / vh);
  const ox = (width - vw * scale) / 2, oy = (height - vh * scale) / 2;
  const X = (x) => ox + (x - vx1) * scale;
  const Y = (y) => height - (oy + (y - vy1) * scale);      // the single y-flip
  if (preview.raster) {
    const image = preview.raster;
    ctx.drawImage(image, X(0), Y(image.naturalHeight),
      image.naturalWidth * scale, image.naturalHeight * scale);
    return { scale, drawn: 1, highlighted: 0 };
  }
  let drawn = 0;
  for (const item of preview.items || []) {
    const color = layerColor(item.layer, item.datatype);
    ctx.fillStyle = color.fill;
    ctx.strokeStyle = color.stroke;
    ctx.lineWidth = 1;
    if (item.kind === "box") {
      const [x1, y1, x2, y2] = item.bbox_dbu;
      const px = X(x1), py = Y(y2), pw = (x2 - x1) * scale, ph = (y2 - y1) * scale;
      ctx.fillRect(px, py, Math.max(pw, 1), Math.max(ph, 1));
      ctx.strokeRect(px, py, Math.max(pw, 1), Math.max(ph, 1));
      drawn += 1;
    } else if (item.kind === "polygon") {
      ctx.beginPath();
      tracePolygon(ctx, item.hull_dbu, X, Y);
      for (const hole of item.holes_dbu || []) tracePolygon(ctx, hole, X, Y);
      ctx.fill("evenodd");
      ctx.stroke();
      drawn += 1;
    } else if (item.kind === "path") {
      const points = item.points_dbu || [];
      if (points.length === 0) continue;
      ctx.beginPath();
      ctx.moveTo(X(points[0][0]), Y(points[0][1]));
      for (let i = 1; i < points.length; i += 1) ctx.lineTo(X(points[i][0]), Y(points[i][1]));
      ctx.lineWidth = Math.max(1, item.width_dbu * scale);
      ctx.lineCap = item.round_ends ? "round" : "butt";
      ctx.lineJoin = item.round_ends ? "round" : "miter";
      ctx.strokeStyle = color.fill;
      ctx.stroke();
      // Square extensions are drawn as short caps at each end.
      if (!item.round_ends && points.length >= 2) {
        extend(ctx, points[1], points[0], item.begin_ext_dbu, item.width_dbu, scale, X, Y);
        extend(ctx, points[points.length - 2], points[points.length - 1], item.end_ext_dbu, item.width_dbu, scale, X, Y);
      }
      drawn += 1;
    }
  }
  if (highlights && highlights.length) {
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#d0342c";
    for (const [x1, y1, x2, y2] of highlights) {
      const px = X(x1) - 3, py = Y(y2) - 3;
      ctx.strokeRect(px, py, Math.max((x2 - x1) * scale, 1) + 6, Math.max((y2 - y1) * scale, 1) + 6);
    }
    ctx.restore();
  }
  return { scale, drawn, highlighted: highlights ? highlights.length : 0 };
}

function tracePolygon(ctx, points, X, Y) {
  if (!points || points.length === 0) return;
  ctx.moveTo(X(points[0][0]), Y(points[0][1]));
  for (let i = 1; i < points.length; i += 1) ctx.lineTo(X(points[i][0]), Y(points[i][1]));
  ctx.closePath();
}

function extend(ctx, from, to, ext, width, scale, X, Y) {
  if (!ext || ext <= 0) return;
  const dx = to[0] - from[0], dy = to[1] - from[1];
  const len = Math.hypot(dx, dy) || 1;
  const ex = to[0] + (dx / len) * ext, ey = to[1] + (dy / len) * ext;
  ctx.beginPath();
  ctx.moveTo(X(to[0]), Y(to[1]));
  ctx.lineTo(X(ex), Y(ey));
  ctx.lineWidth = Math.max(1, width * scale);
  ctx.stroke();
}
