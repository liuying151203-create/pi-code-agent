# s16 新增：用户手动构建；Go 只用于 Zoekt 薄适配器，Agent 和检索流程仍为 Python。
FROM golang:1.26.5-alpine AS build
RUN apk add --no-cache git
WORKDIR /zoekt
RUN git init && git remote add origin https://github.com/sourcegraph/zoekt.git \
    && git fetch --depth=1 origin 27517a30ae6df16c6e768623847fdab8f6a3bbcf \
    && git checkout --detach FETCH_HEAD
COPY search.go cmd/zero2pi-search/main.go
RUN CGO_ENABLED=0 go build -trimpath -o /out/zoekt-index ./cmd/zoekt-index \
    && CGO_ENABLED=0 go build -trimpath -o /out/search ./cmd/zero2pi-search

FROM scratch
LABEL zero2pi.zoekt.commit="27517a30ae6df16c6e768623847fdab8f6a3bbcf"
LABEL zero2pi.zoekt.adapter="s16-v1"
COPY --from=build /out/zoekt-index /zoekt-index
COPY --from=build /out/search /search
COPY --from=build /zoekt/LICENSE /licenses/zoekt-LICENSE
ENTRYPOINT ["/search"]
