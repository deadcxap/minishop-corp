/** Calendar arithmetic in the administrator's browser zone; never add fixed 24h months. */
export function calendarDay(value = new Date()): string {
  return `${String(value.getFullYear()).padStart(4, "0")}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}
function parseDay(value: string): Date {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new RangeError("invalid_day");
  const day = new Date(`${value}T12:00:00`);
  if (!Number.isFinite(day.getTime()) || calendarDay(day) !== value) throw new RangeError("invalid_day");
  return day;
}
export function expiryDay(value: string): string {
  // The exclusive midnight boundary belongs to the preceding paid calendar day.
  return calendarDay(new Date(Date.parse(value) - 1));
}
export function endOfDay(value: string): string {
  const day = parseDay(value);
  day.setDate(day.getDate() + 1);
  day.setHours(0, 0, 0, 0);
  return day.toISOString();
}
export function addPeriod(value: string, days: number, months: number): string {
  const day = parseDay(value);
  if (months) {
    const date = day.getDate();
    day.setDate(1); day.setMonth(day.getMonth() + months);
    const last = new Date(day.getFullYear(), day.getMonth() + 1, 0, 12).getDate();
    day.setDate(Math.min(date, last));
  }
  day.setDate(day.getDate() + days);
  return calendarDay(day);
}
