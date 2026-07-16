/**
 * Local mirror of Meta's template *creation* component format and its
 * validation rules. Source of truth:
 * api/services/messaging/whatsapp/template_service.py
 * (validate_template_definition) — keep the two in sync.
 */

export type NamedExample = { param_name: string; example: string };

export type TemplateComponentExample = {
    header_text?: string[];
    header_text_named_params?: NamedExample[];
    body_text?: string[][];
    body_text_named_params?: NamedExample[];
};

export type TemplateButton = {
    type: string;
    text?: string;
    url?: string;
    phone_number?: string;
    example?: string[];
};

export type TemplateComponent = {
    type: string;
    format?: string;
    text?: string;
    example?: TemplateComponentExample;
    buttons?: TemplateButton[];
};

export type ParameterFormat = 'positional' | 'named';
export type ButtonType = 'QUICK_REPLY' | 'URL' | 'PHONE_NUMBER';

export type ButtonDraft = {
    key: string;
    type: ButtonType;
    text: string;
    url: string;
    phoneNumber: string;
    /** Example value for the trailing URL variable, when present. */
    example: string;
};

export type TemplateDraft = {
    name: string;
    language: string;
    category: string;
    parameterFormat: ParameterFormat;
    headerEnabled: boolean;
    headerText: string;
    headerExample: string;
    bodyText: string;
    bodyExamples: Record<string, string>;
    footerEnabled: boolean;
    footerText: string;
    buttons: ButtonDraft[];
};

export const MAX_BODY_LENGTH = 1024;
export const MAX_HEADER_LENGTH = 60;
export const MAX_FOOTER_LENGTH = 60;
export const MAX_BUTTONS = 10;
export const MAX_URL_BUTTONS = 2;
export const MAX_BUTTON_TEXT_LENGTH = 25;

export const TEMPLATE_NAME_RE = /^[a-z0-9_]{1,512}$/;
export const NAMED_PARAM_RE = /^[a-z][a-z0-9_]*$/;
const PLACEHOLDER_RE = /\{\{\s*([^{}\s]+)\s*\}\}/g;

export const TEMPLATE_CATEGORIES = ['UTILITY', 'MARKETING', 'AUTHENTICATION'] as const;

export const TEMPLATE_LANGUAGES: { value: string; label: string }[] = [
    { value: 'fr', label: 'Français (fr)' },
    { value: 'ar', label: 'Arabe (ar)' },
    { value: 'en', label: 'Anglais (en)' },
    { value: 'en_US', label: 'Anglais — États-Unis (en_US)' },
    { value: 'en_GB', label: 'Anglais — Royaume-Uni (en_GB)' },
    { value: 'es', label: 'Espagnol (es)' },
    { value: 'es_ES', label: 'Espagnol — Espagne (es_ES)' },
    { value: 'pt_BR', label: 'Portugais — Brésil (pt_BR)' },
    { value: 'de', label: 'Allemand (de)' },
    { value: 'it', label: 'Italien (it)' },
    { value: 'nl', label: 'Néerlandais (nl)' },
    { value: 'hi', label: 'Hindi (hi)' },
];

// Mirror of template_service.LOCALLY_EDITABLE_STATUSES.
export const LOCALLY_EDITABLE_STATUSES = ['DRAFT', 'REJECTED', 'PAUSED'];
export const SUBMITTABLE_STATUSES = ['DRAFT', 'REJECTED'];

/**
 * The shared wrapper returns parsed backend JSON; list endpoints may hand
 * back either the array itself or the enveloped `{ <key>: [...] }` shape.
 * Normalize both so this page is agnostic to the wrapper's choice.
 */
export function extractRows<T>(data: unknown, key: string): T[] {
    if (Array.isArray(data)) return data as T[];
    if (data && typeof data === 'object') {
        const nested = (data as Record<string, unknown>)[key];
        if (Array.isArray(nested)) return nested as T[];
    }
    return [];
}

/** All {{token}} occurrences in order of appearance (with duplicates). */
export function scanPlaceholders(text: string): string[] {
    const tokens: string[] = [];
    for (const match of (text || '').matchAll(PLACEHOLDER_RE)) {
        tokens.push(match[1]);
    }
    return tokens;
}

const isDigits = (token: string) => /^\d+$/.test(token);

/**
 * Unique placeholder keys for one text field, filtered to the given
 * parameter format. Positional keys are numerically sorted.
 */
export function formatPlaceholders(text: string, format: ParameterFormat): string[] {
    const isNamed = format === 'named';
    const tokens: string[] = [];
    for (const token of scanPlaceholders(text)) {
        if (isDigits(token) === isNamed) continue;
        if (!tokens.includes(token)) tokens.push(token);
    }
    if (!isNamed) tokens.sort((a, b) => Number(a) - Number(b));
    return tokens;
}

/** Replace every {{token}} that has a non-empty value; keep the rest visible. */
export function substitutePlaceholders(text: string, values: Record<string, string>): string {
    return (text || '').replace(PLACEHOLDER_RE, (whole, token: string) => {
        const value = values[token];
        return value !== undefined && value !== '' ? value : whole;
    });
}

export function emptyDraft(): TemplateDraft {
    return {
        name: '',
        language: 'fr',
        category: 'UTILITY',
        parameterFormat: 'positional',
        headerEnabled: false,
        headerText: '',
        headerExample: '',
        bodyText: '',
        bodyExamples: {},
        footerEnabled: false,
        footerText: '',
        buttons: [],
    };
}

let buttonKeyCounter = 0;
export function newButtonKey(): string {
    buttonKeyCounter += 1;
    return `btn_${buttonKeyCounter}_${Date.now()}`;
}

function urlWithoutPlaceholder(url: string): string {
    return (url || '').replace(PLACEHOLDER_RE, '');
}

/**
 * Rebuild a builder draft from stored components (edit flow). The URL
 * button example is stored as the full example URL — recover the variable
 * value by stripping the static URL prefix.
 */
export function draftFromComponents(
    components: TemplateComponent[],
    parameterFormat: ParameterFormat,
): Omit<TemplateDraft, 'name' | 'language' | 'category' | 'parameterFormat'> {
    const header = components.find((c) => (c.type || '').toUpperCase() === 'HEADER');
    const body = components.find((c) => (c.type || '').toUpperCase() === 'BODY');
    const footer = components.find((c) => (c.type || '').toUpperCase() === 'FOOTER');
    const buttonsComponent = components.find(
        (c) => (c.type || '').toUpperCase() === 'BUTTONS',
    );

    let headerExample = '';
    if (header) {
        if (parameterFormat === 'named') {
            headerExample = header.example?.header_text_named_params?.[0]?.example ?? '';
        } else {
            headerExample = header.example?.header_text?.[0] ?? '';
        }
    }

    const bodyText = body?.text ?? '';
    const bodyExamples: Record<string, string> = {};
    const bodyTokens = formatPlaceholders(bodyText, parameterFormat);
    if (parameterFormat === 'named') {
        for (const entry of body?.example?.body_text_named_params ?? []) {
            if (entry?.param_name) bodyExamples[entry.param_name] = entry.example ?? '';
        }
    } else {
        const row = body?.example?.body_text?.[0] ?? [];
        bodyTokens.forEach((token, index) => {
            bodyExamples[token] = row[index] ?? '';
        });
    }

    const buttons: ButtonDraft[] = (buttonsComponent?.buttons ?? []).map((button) => {
        const type = (button.type || 'QUICK_REPLY').toUpperCase() as ButtonType;
        const url = button.url ?? '';
        let example = '';
        if (type === 'URL' && formatPlaceholders(url, parameterFormat).length > 0) {
            const full = button.example?.[0] ?? '';
            const prefix = urlWithoutPlaceholder(url);
            example = prefix && full.startsWith(prefix) ? full.slice(prefix.length) : full;
        }
        return {
            key: newButtonKey(),
            type,
            text: button.text ?? '',
            url,
            phoneNumber: button.phone_number ?? '',
            example,
        };
    });

    return {
        headerEnabled: !!header,
        headerText: header?.text ?? '',
        headerExample,
        bodyText,
        bodyExamples,
        footerEnabled: !!footer,
        footerText: footer?.text ?? '',
        buttons,
    };
}

/**
 * Assemble the components JSON in Meta's creation shape, embedding the
 * example values (body_text / body_text_named_params / header_text /
 * full-URL button example) the backend validation requires.
 */
export function buildComponents(draft: TemplateDraft): TemplateComponent[] {
    const fmt = draft.parameterFormat;
    const components: TemplateComponent[] = [];

    if (draft.headerEnabled && draft.headerText.trim()) {
        const header: TemplateComponent = {
            type: 'HEADER',
            format: 'TEXT',
            text: draft.headerText,
        };
        const tokens = formatPlaceholders(draft.headerText, fmt);
        if (tokens.length > 0) {
            header.example =
                fmt === 'named'
                    ? {
                        header_text_named_params: tokens.map((token) => ({
                            param_name: token,
                            example: draft.headerExample,
                        })),
                    }
                    : { header_text: [draft.headerExample] };
        }
        components.push(header);
    }

    const body: TemplateComponent = { type: 'BODY', text: draft.bodyText };
    const bodyTokens = formatPlaceholders(draft.bodyText, fmt);
    if (bodyTokens.length > 0) {
        body.example =
            fmt === 'named'
                ? {
                    body_text_named_params: bodyTokens.map((token) => ({
                        param_name: token,
                        example: draft.bodyExamples[token] ?? '',
                    })),
                }
                : { body_text: [bodyTokens.map((token) => draft.bodyExamples[token] ?? '')] };
    }
    components.push(body);

    if (draft.footerEnabled && draft.footerText.trim()) {
        components.push({ type: 'FOOTER', text: draft.footerText });
    }

    if (draft.buttons.length > 0) {
        const buttons: TemplateButton[] = draft.buttons.map((button) => {
            if (button.type === 'URL') {
                const built: TemplateButton = {
                    type: 'URL',
                    text: button.text,
                    url: button.url,
                };
                const tokens = formatPlaceholders(button.url, fmt);
                if (tokens.length > 0) {
                    // Meta expects the example as the full URL with the
                    // variable substituted. Function replacement keeps `$`
                    // sequences in the example value literal.
                    built.example = [
                        button.url.replace(PLACEHOLDER_RE, () => button.example),
                    ];
                }
                return built;
            }
            if (button.type === 'PHONE_NUMBER') {
                return {
                    type: 'PHONE_NUMBER',
                    text: button.text,
                    phone_number: button.phoneNumber,
                };
            }
            return { type: 'QUICK_REPLY', text: button.text };
        });
        components.push({ type: 'BUTTONS', buttons });
    }

    return components;
}

function placeholderFormatErrors(
    label: string,
    text: string,
    fmt: ParameterFormat,
): string[] {
    const errors: string[] = [];
    const seen = new Set<string>();
    for (const token of scanPlaceholders(text)) {
        if (seen.has(token)) continue;
        seen.add(token);
        if (fmt === 'positional' && !isDigits(token)) {
            errors.push(
                `${label} : la variable {{${token}}} est nommée alors que le format est positionnel.`,
            );
        } else if (fmt === 'named') {
            if (isDigits(token)) {
                errors.push(
                    `${label} : la variable {{${token}}} est numérique alors que le format est nommé.`,
                );
            } else if (!NAMED_PARAM_RE.test(token)) {
                errors.push(
                    `${label} : le nom de variable « ${token} » est invalide (minuscules, chiffres, underscores, commençant par une lettre).`,
                );
            }
        }
    }
    return errors;
}

function sequentialError(label: string, tokens: string[]): string | null {
    const numbers = [...new Set(tokens.map(Number))].sort((a, b) => a - b);
    for (let i = 0; i < numbers.length; i += 1) {
        if (numbers[i] !== i + 1) {
            return `${label} : les variables positionnelles doivent être séquentielles ({{1}}, {{2}}, …).`;
        }
    }
    return null;
}

/**
 * Client-side mirror of validate_template_definition — French messages.
 * Empty list means the draft can be submitted.
 */
export function validateDraft(draft: TemplateDraft): string[] {
    const errors: string[] = [];
    const fmt = draft.parameterFormat;

    if (!TEMPLATE_NAME_RE.test(draft.name)) {
        errors.push(
            'Le nom doit contenir uniquement des minuscules, chiffres et underscores (ex. confirmation_commande).',
        );
    }

    const bodyText = draft.bodyText;
    if (!bodyText.trim()) {
        errors.push('Le corps du message est requis.');
    } else {
        if (bodyText.length > MAX_BODY_LENGTH) {
            errors.push(`Le corps dépasse ${MAX_BODY_LENGTH} caractères (${bodyText.length}).`);
        }
        errors.push(...placeholderFormatErrors('Corps', bodyText, fmt));
        const bodyTokens = formatPlaceholders(bodyText, fmt);
        if (fmt === 'positional' && bodyTokens.length > 0) {
            const sequential = sequentialError('Corps', bodyTokens);
            if (sequential) errors.push(sequential);
        }
        for (const token of bodyTokens) {
            if (!(draft.bodyExamples[token] ?? '').trim()) {
                errors.push(`Corps : une valeur d'exemple est requise pour {{${token}}}.`);
            }
        }
    }

    if (draft.headerEnabled) {
        const headerText = draft.headerText;
        if (!headerText.trim()) {
            errors.push("Le texte de l'en-tête est requis (ou désactivez l'en-tête).");
        } else {
            if (headerText.length > MAX_HEADER_LENGTH) {
                errors.push(
                    `L'en-tête dépasse ${MAX_HEADER_LENGTH} caractères (${headerText.length}).`,
                );
            }
            errors.push(...placeholderFormatErrors('En-tête', headerText, fmt));
            const headerTokens = formatPlaceholders(headerText, fmt);
            if (headerTokens.length > 1) {
                errors.push("L'en-tête accepte au maximum 1 variable.");
            }
            if (fmt === 'positional' && headerTokens.length === 1 && headerTokens[0] !== '1') {
                errors.push("La variable de l'en-tête doit être {{1}}.");
            }
            if (headerTokens.length > 0 && !draft.headerExample.trim()) {
                errors.push("En-tête : une valeur d'exemple est requise pour la variable.");
            }
        }
    }

    if (draft.footerEnabled) {
        if (draft.footerText.length > MAX_FOOTER_LENGTH) {
            errors.push(
                `Le pied de page dépasse ${MAX_FOOTER_LENGTH} caractères (${draft.footerText.length}).`,
            );
        }
        if (scanPlaceholders(draft.footerText).length > 0) {
            errors.push('Le pied de page ne doit pas contenir de variables.');
        }
    }

    if (draft.buttons.length > MAX_BUTTONS) {
        errors.push(`Au maximum ${MAX_BUTTONS} boutons sont autorisés.`);
    }
    let urlCount = 0;
    draft.buttons.forEach((button, index) => {
        const label = `Bouton ${index + 1}`;
        if (!button.text.trim()) {
            errors.push(`${label} : le libellé est requis.`);
        } else if (button.text.length > MAX_BUTTON_TEXT_LENGTH) {
            errors.push(
                `${label} : le libellé dépasse ${MAX_BUTTON_TEXT_LENGTH} caractères.`,
            );
        }
        if (button.type === 'URL') {
            urlCount += 1;
            if (!button.url.trim()) {
                errors.push(`${label} : l'URL est requise.`);
            } else {
                errors.push(...placeholderFormatErrors(`${label} (URL)`, button.url, fmt));
                const tokens = formatPlaceholders(button.url, fmt);
                if (tokens.length > 0) {
                    if (tokens.length > 1 || !button.url.trimEnd().endsWith('}}')) {
                        errors.push(
                            `${label} : une seule variable est autorisée, placée en fin d'URL.`,
                        );
                    } else if (fmt === 'positional' && tokens[0] !== '1') {
                        errors.push(`${label} : la variable de l'URL doit être {{1}}.`);
                    }
                    if (!button.example.trim()) {
                        errors.push(
                            `${label} : une valeur d'exemple est requise pour la variable de l'URL.`,
                        );
                    }
                }
            }
        } else if (button.type === 'PHONE_NUMBER' && !button.phoneNumber.trim()) {
            errors.push(`${label} : le numéro de téléphone est requis.`);
        }
    });
    if (urlCount > MAX_URL_BUTTONS) {
        errors.push(`Au maximum ${MAX_URL_BUTTONS} boutons URL sont autorisés (${urlCount}).`);
    }

    return errors;
}
