import type { JobSettings } from "../api/queries";

export type Condition = JobSettings["conditions"][number];

export const CONDITIONS: { value: Condition; label: string }[] = [
  { value: "new_new", label: "New" },
  { value: "new_open_box", label: "New, open box" },
  { value: "new_oem", label: "New, OEM" },
  { value: "refurbished_refurbished", label: "Refurbished" },
  { value: "used_like_new", label: "Used, like new" },
  { value: "used_very_good", label: "Used, very good" },
  { value: "used_good", label: "Used, good" },
  { value: "used_acceptable", label: "Used, acceptable" },
  { value: "collectible_like_new", label: "Collectible, like new" },
  { value: "collectible_very_good", label: "Collectible, very good" },
  { value: "collectible_good", label: "Collectible, good" },
  { value: "collectible_acceptable", label: "Collectible, acceptable" },
  { value: "club_club", label: "Club" },
];

export const conditionLabel = (code: string | null | undefined) =>
  CONDITIONS.find((c) => c.value === code)?.label ?? code ?? "—";
