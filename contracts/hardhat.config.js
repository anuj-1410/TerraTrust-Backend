require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config({ path: "../.env" });

/** @type import('hardhat/config').HardhatUserConfig */

module.exports = {
  solidity: {
    compilers: [
      {
        version: "0.8.29",
        settings: {
          optimizer: {
            enabled: true,
            runs: 200
          },
          evmVersion: "cancun"
        }
      }
    ]
  },

  defaultNetwork: "hardhat",

  networks: {
    polygon_amoy: {
      url: process.env.ALCHEMY_POLYGON_AMOY_URL || "",
      accounts: process.env.ADMIN_WALLET_PRIVATE_KEY
        ? [process.env.ADMIN_WALLET_PRIVATE_KEY]
        : [],
      chainId: 80002
    }
  },

  etherscan: {
    apiKey: process.env.POLYGONSCAN_API_KEY,
    customChains: [
      {
        network: "polygon_amoy",
        chainId: 80002,
        urls: {
          apiURL: "https://api-amoy.polygonscan.com/api",
          browserURL: "https://amoy.polygonscan.com"
        }
      }
    ]
  },

  paths: {
    sources: "./contracts",
    tests: "./test",
    cache: "./cache",
    artifacts: "./artifacts"
  }
};