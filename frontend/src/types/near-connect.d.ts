declare module "@hot-labs/near-connect" {
  export type NearNetwork = "mainnet" | "testnet"

  export type NearAccount = {
    accountId: string
    publicKey?: string
  }

  export type NearSignedMessage = {
    accountId: string
    publicKey: string
    signature: string
  }

  export type NearWallet = {
    getAccounts: (options?: { network?: NearNetwork }) => Promise<NearAccount[]>
    signMessage: (options: {
      message: string
      recipient: string
      nonce: Uint8Array
      network?: NearNetwork
    }) => Promise<NearSignedMessage>
    signAndSendTransaction: (options: {
      network?: NearNetwork
      signerId?: string
      receiverId: string
      actions: Array<
        | {
            type: "Transfer"
            params: { deposit: string }
          }
        | {
            type: "FunctionCall"
            params: {
              methodName: string
              args: object
              gas: string
              deposit: string
            }
          }
      >
    }) => Promise<unknown>
  }

  export type NearConnectorOptions = {
    network?: NearNetwork
    autoConnect?: boolean
    footerBranding?: null
    manifest?: unknown
    features?: Record<string, boolean>
  }

  export class NearConnector {
    constructor(options?: NearConnectorOptions)
    connect: () => Promise<NearWallet>
    wallet: () => Promise<NearWallet>
    disconnect: () => Promise<void>
  }
}
