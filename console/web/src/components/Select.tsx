"use client";

import * as React from "react";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import styles from "./Select.module.css";

export type SelectOption = {
  value: string;
  label: React.ReactNode;
  disabled?: boolean;
};

export type SelectProps = {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  id?: string;
  name?: string;
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  placeholder?: string;
};

// Radix reserves "" for its placeholder. Prefix every value, including "",
// so an application's real option value can never collide with the sentinel.
const VALUE_PREFIX = "strixops-select:";

export function Select({
  value,
  onValueChange,
  options,
  id,
  name,
  disabled = false,
  className,
  style,
  placeholder,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
}: SelectProps) {
  const selected = options.some((option) => option.value === value);
  const isDisabled = disabled || options.length === 0;

  return (
    <>
      <SelectPrimitive.Root
        value={selected ? VALUE_PREFIX + value : ""}
        onValueChange={(next) => onValueChange(next.slice(VALUE_PREFIX.length))}
        disabled={isDisabled}
      >
        <SelectPrimitive.Trigger
          id={id}
          className={[styles.trigger, className].filter(Boolean).join(" ")}
          style={style}
          aria-label={ariaLabel}
          aria-labelledby={ariaLabelledBy}
        >
          <span className={styles.value}>
            <SelectPrimitive.Value placeholder={placeholder} />
          </span>
          <SelectPrimitive.Icon className={styles.icon}>
            <ChevronDown size={14} aria-hidden="true" />
          </SelectPrimitive.Icon>
        </SelectPrimitive.Trigger>
        <SelectPrimitive.Portal>
          <SelectPrimitive.Content
            className={styles.content}
            position="popper"
            align="start"
            sideOffset={6}
            collisionPadding={12}
          >
            <SelectPrimitive.ScrollUpButton className={styles.scrollButton}>
              <ChevronUp size={14} aria-hidden="true" />
            </SelectPrimitive.ScrollUpButton>
            <SelectPrimitive.Viewport className={styles.viewport}>
              {options.map((option) => (
                <SelectPrimitive.Item
                  key={option.value}
                  value={VALUE_PREFIX + option.value}
                  disabled={option.disabled}
                  className={styles.item}
                >
                  <SelectPrimitive.ItemIndicator className={styles.indicator}>
                    <Check size={13} strokeWidth={2.5} aria-hidden="true" />
                  </SelectPrimitive.ItemIndicator>
                  <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
                </SelectPrimitive.Item>
              ))}
            </SelectPrimitive.Viewport>
            <SelectPrimitive.ScrollDownButton className={styles.scrollButton}>
              <ChevronDown size={14} aria-hidden="true" />
            </SelectPrimitive.ScrollDownButton>
          </SelectPrimitive.Content>
        </SelectPrimitive.Portal>
      </SelectPrimitive.Root>
      {name && <input type="hidden" name={name} value={value} disabled={isDisabled} />}
    </>
  );
}
