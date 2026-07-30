# syntax=docker/dockerfile:1
# Multi-stage build
# Stage 1: Dependencies
FROM node:22-alpine AS deps
WORKDIR /app

# Install Python and build dependencies for native modules
# This helps with ARM64 builds and native module compilation
RUN apk add --no-cache python3 make g++ libc6-compat

# Copy package files
COPY ui/package*.json ./

# Clean install with proper handling of native modules
RUN --mount=type=cache,target=/root/.npm npm ci

# Stage 2: Builder
FROM node:22-alpine AS builder
WORKDIR /app

# Install libc6-compat for native modules in builder stage too
RUN apk add --no-cache libc6-compat

# Copy dependencies from deps stage
COPY --from=deps /app/node_modules ./node_modules

# Copy all files needed for build
COPY ui/package*.json ./
COPY ui/tsconfig.json ./
COPY ui/next.config.ts ./
COPY ui/components.json ./
COPY ui/sentry.edge.config.ts ./
COPY ui/sentry.server.config.ts ./
COPY ui/postcss.config.mjs ./
COPY ui/public ./public
COPY ui/src ./src

# Set build-time environment variables (needed for Next.js build)
ENV NEXT_PUBLIC_NODE_ENV="oss"
ENV NEXT_TELEMETRY_DISABLED="1"
ENV BACKEND_URL="http://api:8000"

# Support desk. Empty on purpose: the upstream values pointed the floating chat
# bubble at the Dograh helpdesk with a shared token, which would have routed
# THIS deployment's customer conversations into someone else's inbox.
# ChatwootWidget bails out and renders nothing unless both are set, so a Volira
# helpdesk is opt-in — pass both as build args to turn the bubble back on.
ARG NEXT_PUBLIC_CHATWOOT_URL=""
ARG NEXT_PUBLIC_CHATWOOT_TOKEN=""
ENV NEXT_PUBLIC_CHATWOOT_URL=$NEXT_PUBLIC_CHATWOOT_URL
ENV NEXT_PUBLIC_CHATWOOT_TOKEN=$NEXT_PUBLIC_CHATWOOT_TOKEN

# Brand contact points consumed by src/lib/support.ts. Next inlines NEXT_PUBLIC_*
# at build time, so setting these in docker-compose at runtime would do nothing —
# they have to arrive here, as build args.
ARG NEXT_PUBLIC_SUPPORT_EMAIL=""
ARG NEXT_PUBLIC_DOCS_URL=""
ARG NEXT_PUBLIC_TERMS_URL=""
ARG NEXT_PUBLIC_PRIVACY_URL=""
ENV NEXT_PUBLIC_SUPPORT_EMAIL=$NEXT_PUBLIC_SUPPORT_EMAIL
ENV NEXT_PUBLIC_DOCS_URL=$NEXT_PUBLIC_DOCS_URL
ENV NEXT_PUBLIC_TERMS_URL=$NEXT_PUBLIC_TERMS_URL
ENV NEXT_PUBLIC_PRIVACY_URL=$NEXT_PUBLIC_PRIVACY_URL

# Build the application with standalone mode
# Increase Node.js heap size to prevent out-of-memory errors during build
ENV NODE_OPTIONS="--max-old-space-size=4096"
RUN npm run build && \
    rm -rf /tmp/* /root/.npm /root/.next/cache

# Stage 3: Runner (production image)
FROM node:22-alpine AS runner
WORKDIR /app

# Environment variables will be provided by docker-compose
ENV NODE_ENV=production

# Create a non-root user
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy standalone build output
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

# Switch to non-root user
USER nextjs

# Expose the port Next.js runs on
EXPOSE 3010

# Start the production server using the standalone Node.js server
CMD sh -c "echo '🚀 Application ready at http://localhost:3010' && PORT=3010 node server.js"
