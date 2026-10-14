import * as SwitchPrimitive from "@radix-ui/react-switch";
import { cn } from "@/lib/utils";

/** The one on/off control: Radix's behaviour (click, Space, `aria-checked`,
 *  keyboard) with the app's accent token as the checked fill. It exists so a
 *  Settings boolean reads as "label ……… [switch]" — the same shape the select
 *  and number rows already use — instead of a checkbox whose tick sits in a
 *  different place on every row. */
export default function Switch({ className, ...props }: SwitchPrimitive.SwitchProps) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "relative inline-flex h-[18px] w-[32px] shrink-0 cursor-pointer items-center rounded-full border border-border bg-raise transition-colors",
        // A phone-width hit area (index.css): the control stays 18px, the
        // thumb gets 32px — the floor check_responsive sweeps for.
        "switch-hit",
        "data-[state=checked]:border-accent data-[state=checked]:bg-accent",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/55",
        "disabled:cursor-default disabled:opacity-40",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "pointer-events-none block h-[14px] w-[14px] rounded-full bg-zinc-500 transition-transform",
          "translate-x-[2px] data-[state=checked]:translate-x-[16px] data-[state=checked]:bg-accent-fg"
        )}
      />
    </SwitchPrimitive.Root>
  );
}