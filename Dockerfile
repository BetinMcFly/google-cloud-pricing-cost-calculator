# syntax=docker/dockerfile:1
#
# gcosts - Google Cloud Platform Pricing and Cost Calculator
#
# Imagen para ejecutar `gcosts calc` como Cloud Run Job.
# El pricing.yml se hornea en la imagen: el digest identifica el juego de precios
# exacto con el que se calculo una propuesta. Ver deploy/PLAN.md.

# ---------- build ----------
FROM golang:1.24 AS build
WORKDIR /src

# Dependencias primero, para aprovechar la cache de capas
COPY gcosts/go.mod gcosts/go.sum ./gcosts/
RUN cd gcosts && go mod download

COPY gcosts/ ./gcosts/

# Mismos ldflags que gcosts/Makefile
ARG VERSION=container
RUN cd gcosts && CGO_ENABLED=0 GOOS=linux go build \
      -ldflags "-w -s -X github.com/Cyclenerd/google-cloud-pricing-cost-calculator/gcosts/cmd.version=${VERSION}" \
      -o /out/gcosts

# ---------- runtime ----------
# distroless: sin shell, sin gestor de paquetes, sin Perl ni SQLite.
# El pipeline de precios (build/) es build-time del repo, no del contenedor.
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/gcosts /gcosts
COPY pricing.yml /pricing.yml

# Sin CMD: cada ejecucion del job pasa sus propios argumentos.
ENTRYPOINT ["/gcosts"]
