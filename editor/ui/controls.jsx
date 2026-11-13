import { cva } from 'class-variance-authority';
import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';
import { ChevronDown } from 'lucide-react';

// Local shadcn/ui New York primitives. The native variants keep form semantics and IDs.
function cn(...values) {
  return twMerge(clsx(values));
}

const buttonVariants = cva('ui-button', {
  variants: {
    variant: {
      default: 'ui-primary',
      outline: '',
      ghost: 'ui-ghost',
      destructive: 'ui-destructive',
    },
    size: { default: '', icon: 'ui-icon' },
  },
  defaultVariants: { variant: 'outline', size: 'default' },
});

/** Render a shadcn button with a shared size and emphasis scale. */
export function Button({ className, variant, size, ...props }) {
  return (
    <button
      data-slot="button"
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  );
}

/** Render a native input with shared validation and focus styles. */
export function Input({ className, ...props }) {
  return <input data-slot="input" className={cn('ui-input', className)} {...props} />;
}

/** Keep platform selection, type-ahead and keyboard behavior. */
export function NativeSelect({ className, children, ...props }) {
  return (
    <span data-slot="native-select-wrapper" className="ui-select-wrap">
      <select data-slot="native-select" className={cn('ui-select', className)} {...props}>
        {children}
      </select>
      <ChevronDown aria-hidden="true" />
    </span>
  );
}

/** Render a native multiline field. */
export function Textarea({ className, ...props }) {
  return <textarea data-slot="textarea" className={cn('ui-textarea', className)} {...props} />;
}

/** Retain table semantics for game data. */
export function Table({ className, ...props }) {
  return <table data-slot="table" className={cn('ui-table', className)} {...props} />;
}
